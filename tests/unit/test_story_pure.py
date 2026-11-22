"""故事模块的纯逻辑：令牌桶、神谕、状态卡、世界种子、转录渲染。"""

from __future__ import annotations

import json
import random
from datetime import datetime
from pathlib import Path

import pytest

from astrbot_plugin_shadow_memory.story import bucket, card, oracle, prompts, world

DAY = 86_400_000
HOUR = 3_600_000
MINUTE = 60_000


def at(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> int:
    return int(datetime(year, month, day, hour, minute).timestamp() * 1000)


# --------------------------------------------------------------------- 令牌桶
def test_bucket_refills_evenly_and_caps():
    assert bucket.refill(2.0, 1000, 1000 + DAY // 2, per_day=10, capacity=30) == pytest.approx(7.0)
    assert bucket.refill(28.0, 1000, 1000 + DAY, per_day=10, capacity=30) == 30.0


def test_bucket_ignores_clock_going_backwards_and_missing_stamp():
    assert bucket.refill(3.0, 5000, 1000, per_day=10, capacity=30) == 3.0
    assert bucket.refill(3.0, 0, 9 * DAY, per_day=10, capacity=30) == 3.0
    assert bucket.refill(99.0, 0, 1, per_day=10, capacity=30) == 30.0


def test_bucket_initial_is_one_day_but_never_above_capacity():
    assert bucket.initial(per_day=10, capacity=30) == 10
    assert bucket.initial(per_day=10, capacity=4) == 4


def test_bucket_ms_until_next_token():
    assert bucket.ms_until(1.5, 1, per_day=10) == 0
    assert bucket.ms_until(0.5, 1, per_day=10) == DAY // 20
    assert bucket.ms_until(0.0, 1, per_day=0) is None


# --------------------------------------------------------------------- 神谕
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("很可能", "很可能"),
        (" 几乎不可能 ", "几乎不可能"),
        ("likely", "很可能"),
        ("不可能", "几乎不可能"),
        ("我觉得很可能成", "很可能"),
        ("", "五五开"),
        (None, "五五开"),
        ("谁知道呢", "五五开"),
    ],
)
def test_parse_likelihood(raw, expected):
    assert oracle.parse_likelihood(raw) == expected


class FixedRandom(random.Random):
    """按给定序列出数的随机源。"""

    def __init__(self, values):
        super().__init__(0)
        self._values = list(values)

    def random(self):
        return self._values.pop(0)


def test_roll_uses_declared_likelihood():
    success = oracle.roll("很可能", FixedRandom([0.70, 0.99]))
    assert success.success and not success.twist and success.label == "成了"
    failure = oracle.roll("很可能", FixedRandom([0.80, 0.99]))
    assert not failure.success and failure.label == "没成"


def test_roll_twists():
    assert oracle.roll("五五开", FixedRandom([0.1, 0.01])).label == "成了，但有代价"
    assert oracle.roll("五五开", FixedRandom([0.9, 0.01])).label == "没成，但有转机"


def test_roll_frequencies_follow_the_table():
    rng = random.Random(7)
    for name, chance in oracle.LIKELIHOODS.items():
        hits = sum(oracle.roll(name, rng).success for _ in range(4000))
        assert hits / 4000 == pytest.approx(chance, abs=0.03)


def test_draw_seed_is_reproducible_and_well_formed():
    first = oracle.draw_seed(random.Random(3))
    again = oracle.draw_seed(random.Random(3))
    assert first == again
    assert first.focus in oracle.FOCUSES
    assert len(set(first.words)) == 2 and set(first.words) <= set(oracle.WORDS)


def test_real_moment():
    moment = oracle.real_moment(at(2026, 11, 22, 22, 40))
    assert moment["weekday"] == "星期日"
    assert moment["weekend"] is True
    assert moment["period"] == "晚上"
    assert moment["text"] == "2026年11月22日 星期日 晚上 22:40（周末）"
    holiday = oracle.real_moment(at(2026, 10, 1, 9, 5))
    assert holiday["holiday"] == "国庆节"
    assert "国庆节" in holiday["text"]


@pytest.mark.parametrize(
    ("hour", "period"),
    [(0, "凌晨"), (5, "清晨"), (8, "上午"), (12, "中午"), (14, "下午"), (18, "傍晚"), (21, "晚上"), (23, "深夜")],
)
def test_period_of(hour, period):
    assert oracle.period_of(hour) == period


# --------------------------------------------------------------------- 状态卡
def test_build_card_clamps_minutes_and_reads_phone():
    now = at(2026, 11, 22, 14, 0)
    built = card.build_card(
        {"where": "图书馆三楼", "doing": "写课程论文", "minutes": 150, "phone": "不方便"},
        now,
        max_minutes=720,
    )
    assert built.until == now + 150 * MINUTE
    assert built.phone_ok is False
    long = card.build_card({"where": "家", "doing": "睡觉", "minutes": 99999}, now, max_minutes=720)
    assert long.until == now + 720 * MINUTE
    short = card.build_card({"where": "家", "doing": "发呆", "minutes": "abc"}, now, max_minutes=720)
    assert short.until == now + 5 * MINUTE
    assert short.phone_ok is True


def test_build_card_needs_place_or_activity():
    assert card.build_card({"minutes": 30}, 1, max_minutes=60) is None
    assert card.build_card("图书馆", 1, max_minutes=60) is None


def test_card_roundtrip_through_json():
    now = at(2026, 11, 22, 14, 0)
    built = card.build_card({"where": "图书馆", "doing": "看书", "minutes": 60}, now, max_minutes=720)
    assert card.load_card(built.to_json()) == built
    assert card.load_card("") is None
    assert card.load_card("{bad json") is None
    assert card.load_card('{"until": 5}') is None


def test_render_for_chat_while_active():
    now = at(2026, 11, 22, 14, 0)
    built = card.build_card(
        {"where": "图书馆三楼", "doing": "写课程论文", "minutes": 150, "phone": "不方便"},
        now,
        max_minutes=720,
    )
    assert card.render_for_chat(built, now + 10 * MINUTE) == [
        "- 我现在在图书馆三楼，正在写课程论文，大概到16:30。",
        "- 这会儿不太方便看手机。",
    ]


def test_render_for_chat_busy_expires_with_the_activity():
    """过了预计结束时间，“不方便”立刻失效；再久就整张卡不注入。"""
    now = at(2026, 11, 22, 14, 0)
    built = card.build_card(
        {"where": "考场", "doing": "考试", "minutes": 60, "phone": "不方便"}, now, max_minutes=720
    )
    later = card.render_for_chat(built, now + 2 * HOUR, stale_hours=6)
    assert later == ["- 我之前在考场，正在考试，按说15:00就结束了，这会儿应该已经忙完。"]
    assert not any("不方便" in line for line in later)
    assert card.render_for_chat(built, now + 3 * DAY, stale_hours=6) == []


def test_render_for_chat_without_card():
    assert card.render_for_chat(None, 1) == []


def test_format_clock_across_days():
    now = at(2026, 11, 22, 23, 0)
    assert card.format_clock(at(2026, 11, 22, 23, 30), now) == "23:30"
    assert card.format_clock(at(2026, 11, 23, 8, 0), now) == "明天 08:00"
    assert card.format_clock(at(2026, 11, 25, 8, 0), now) == "11月25日 08:00"


def test_render_for_gm_reports_overrun():
    now = at(2026, 11, 22, 14, 0)
    built = card.build_card({"where": "车上", "doing": "去机场", "minutes": 30}, now, max_minutes=720)
    assert "还有 20 分钟" in card.render_for_gm(built, now + 10 * MINUTE)
    assert "已经过了 30 分钟" in card.render_for_gm(built, now + HOUR)
    assert card.render_for_gm(None, now) == "（还没有状态卡）"


# --------------------------------------------------------------------- 世界种子
def seed(**overrides):
    data = {
        "version": "world_seed/v1",
        "title": "青屿",
        "outline": "她是青屿大学的大三学生，住在学校北门外的合租屋里。" * 3,
        "entries": [
            {"category": "地点", "name": "青屿大学", "body": "一所靠海的综合性大学。"},
            {"category": "person", "name": "阿澄", "body": "她的室友，学美术，作息混乱。"},
        ],
        "hooks": ["期末前要交一份小组报告"],
        "opening": {"where": "合租屋", "doing": "赖床"},
    }
    data.update(overrides)
    return data


def test_validate_seed_normalizes():
    result = world.validate_seed(seed())
    assert result["title"] == "青屿"
    assert [entry["category"] for entry in result["entries"]] == ["place", "person"]
    assert result["hooks"] == ["期末前要交一份小组报告"]
    assert result["opening"] == {"where": "合租屋", "doing": "赖床"}


def test_validate_seed_reports_every_problem_at_once():
    with pytest.raises(world.WorldSeedError) as error:
        world.validate_seed(
            seed(
                version="persona_seed/v1",
                outline="太短",
                entries=[{"name": "", "body": "x"}, {"name": "a", "body": ""}],
            )
        )
    message = str(error.value)
    assert "version" in message and "outline" in message
    assert "entries[0]" in message and "entries[1]" in message


def test_validate_seed_rejects_duplicates_and_non_objects():
    with pytest.raises(world.WorldSeedError, match="重复"):
        world.validate_seed(
            seed(entries=[{"name": "阿澄", "body": "一"}, {"name": "阿澄", "body": "二"}])
        )
    with pytest.raises(world.WorldSeedError):
        world.validate_seed(["不是对象"])


def test_validate_seed_optional_parts_can_be_missing():
    result = world.validate_seed(
        {"version": "world_seed/v1", "outline": "她在一座靠海的小城里读大学。" * 5}
    )
    assert result["entries"] == [] and result["hooks"] == [] and result["opening"] == {}


def test_shipped_example_seed_is_valid():
    path = Path(__file__).resolve().parents[2] / "docs" / "world_seed.example.json"
    result = world.validate_seed(json.loads(path.read_text(encoding="utf-8")))
    assert result["title"] == "青屿" and len(result["entries"]) == 6
    assert {entry["category"] for entry in result["entries"]} == {"place", "person", "rule"}
    assert result["opening"] == {"where": "合租屋的卧室", "doing": "赖床"}


def test_search_prefers_name_hits():
    entries = world.validate_seed(seed())["entries"]
    assert world.search(entries, "她回到合租屋，阿澄还没睡", 1)[0]["name"] == "阿澄"
    assert world.search(entries, "完全无关的内容xyz", 3) == []
    assert world.search(entries, "阿澄", 0) == []


def test_find_by_names_exact_then_loose():
    entries = world.validate_seed(seed())["entries"]
    assert [entry["name"] for entry in world.find_by_names(entries, ["阿澄"])] == ["阿澄"]
    assert [entry["name"] for entry in world.find_by_names(entries, ["青屿"])] == ["青屿大学"]
    assert world.find_by_names(entries, ["不存在", "", None]) == []


def test_clean_canon_and_notes():
    canon = world.clean_canon(
        [
            {"name": "便利店老板", "category": "人物", "body": "姓周，话少。"},
            {"name": "便利店老板", "category": "人物", "body": "姓周，话少。"},
            {"name": "", "body": "没有名字"},
            "不是对象",
        ]
    )
    assert canon == [{"category": "person", "name": "便利店老板", "body": "姓周，话少。"}]
    notes = world.clean_notes(
        [{"title": "暗线", "body": "室友在瞒着她找工作"}, {"title": "旧伏笔", "body": ""}, {"body": "无标题"}]
    )
    assert notes == [
        {"title": "暗线", "body": "室友在瞒着她找工作"},
        {"title": "旧伏笔", "body": ""},
    ]
    assert world.clean_canon(None) == [] and world.clean_notes("x") == []


# --------------------------------------------------------------------- 转录
TRANSCRIPT = [
    {"role": "narrator", "text": "你站在便利店门口，雨下大了。"},
    {"role": "me", "inner": "其实不想回去", "text": "我走进去，想买把伞。"},
]


def test_gm_never_sees_her_inner_thoughts():
    """背景模型只读外显：她心里想的不能出现在给它的转录里。"""
    rendered = prompts.render_transcript(TRANSCRIPT, viewer="gm")
    assert "其实不想回去" not in rendered
    assert rendered == "【旁白】你站在便利店门口，雨下大了。\n【她】我走进去，想买把伞。"


def test_she_and_memory_see_inner_thoughts():
    for viewer in ("me", "memory"):
        rendered = prompts.render_transcript(TRANSCRIPT, viewer=viewer)
        assert "（心里：其实不想回去）我走进去，想买把伞。" in rendered


def test_actor_prompt_assembly():
    system = prompts.actor_system(
        base_card="她叫小影。", persona="我遇事会先观察。", ambient="", moment="晚上 22:40"
    )
    assert system.startswith("她叫小影。\n\n我遇事会先观察。")
    assert "晚上 22:40" in system and '"action"' in system
    user = prompts.actor_user(TRANSCRIPT, ["那次淋雨发烧", "还没想明白的事"])
    assert "我脑子里浮起：那次淋雨发烧" in user
    assert "心里还有一点余波：还没想明白的事" in user
    assert user.endswith("现在轮到我。")
    assert "【我的记忆" not in prompts.actor_user(TRANSCRIPT, [])


def test_gm_task_templates_render():
    turn = prompts.gm_turn_task(last=True)
    assert "几乎不可能 / 不太可能 / 五五开 / 很可能 / 几乎必然" in turn
    assert prompts.GM_TURN_LAST_HINT in turn
    assert prompts.GM_TURN_LAST_HINT not in prompts.gm_turn_task(last=False)
    assert '{"check": null' in turn
    close = prompts.gm_close_task(min_wake=10, max_wake=720)
    assert "10–720" in close and '"card": {"where"' in close
    assert prompts.gm_open_task(first=True, backfill=True) == prompts.GM_OPEN_TASK_FIRST
    assert prompts.gm_open_task(first=False, backfill=True) == prompts.GM_OPEN_TASK_BACKFILL
    assert prompts.gm_open_task(first=False, backfill=False) == prompts.GM_OPEN_TASK_PLAIN
