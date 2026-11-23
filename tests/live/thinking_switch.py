"""背景模型思考开关的三路实测（手动运行，不进 CI）。

四种切法从同一起点各演一场，再把复杂起点的转录和「全程不开思考」盲比。

- off：全程不开思考。
- close：只有收场开思考。
- scene：一次非思考判断认为「有还没了结的事」时，这一场的背景模型调用都开思考。
- plan：同一次判断。认为要时，先用思考写一段只给背景模型看的约束（不写她会做什么），
  开场、裁定、收场都不开思考。

同一重复次数下四种切法共用一个骰子种子。``past_scenes`` 为 0。密钥只从环境变量读。

用法（工作区根目录）：
    $env:DSH_TEST_TEXT_BASE_URL="https://api.deepseek.com/v1"
    $env:DSH_TEST_TEXT_API_KEY="sk-..."
    .venv\\Scripts\\python.exe -X utf8 astrbot_plugin_shadow_memory/tests/live/thinking_switch.py

复杂起点默认用 ``.pytest_tmp/history.db``。结果写在 ``.pytest_tmp/thinking_switch/``，不含密钥。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import random
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from astrbot_plugin_shadow_memory.core import clock as clock_mod  # noqa: E402
from astrbot_plugin_shadow_memory.core.audit import Audit  # noqa: E402
from astrbot_plugin_shadow_memory.core.config import ConfigView  # noqa: E402
from astrbot_plugin_shadow_memory.core.db import Database  # noqa: E402
from astrbot_plugin_shadow_memory.core.decision import chat_url  # noqa: E402
from astrbot_plugin_shadow_memory.core.jsonx import extract_json  # noqa: E402
from astrbot_plugin_shadow_memory.core.textgen import TextClient  # noqa: E402
from astrbot_plugin_shadow_memory.story.card import load_card  # noqa: E402
from astrbot_plugin_shadow_memory.story.director import Director  # noqa: E402
from astrbot_plugin_shadow_memory.story.llm import StoryLLM  # noqa: E402
from astrbot_plugin_shadow_memory.story.store import StoryStore  # noqa: E402
from astrbot_plugin_shadow_memory.story.world import validate_seed  # noqa: E402
from astrbot_plugin_shadow_memory.tests.fakes import FakeJev  # noqa: E402
from astrbot_plugin_shadow_memory.tests.live.smoke_models import UrllibClient  # noqa: E402
from astrbot_plugin_shadow_memory.tests.live.smoke_story import (  # noqa: E402
    BASE_CARD,
    BOT,
    Clock,
    DirectProvider,
    seed_memory,
    world_seed,
)
from astrbot_plugin_shadow_memory.tests.unit.helpers import build_stack  # noqa: E402

OUT = ROOT / ".pytest_tmp" / "thinking_switch"
HISTORY = ROOT / ".pytest_tmp" / "history.db"
GM_MODEL = "deepseek-flash"
ACTOR_MODEL = "deepseek-chat"
ARMS = ("off", "close", "scene", "plan")
ARM_LABEL = {"off": "对照", "close": "A 只在收场", "scene": "B 整场", "plan": "C 先规划"}
PLAIN_STARTS = (
    "2026-11-23T07:30",
    "2026-11-23T09:40",
    "2026-11-23T12:10",
    "2026-11-23T15:20",
    "2026-11-23T18:00",
    "2026-11-23T21:10",
)
SEM = asyncio.Semaphore(6)
FP_LIMIT = 2
MIN_PAIRS = 4

DETECTOR_SYSTEM = """你在决定一场故事的主持人要不要为「已经发生、还没收尾的事」多想一步。
笔记里写着还没了结的事（欠的钱、账单、约好要来的人、答应过还没回的话），填 true。
只有笔记里没有这种事，或者这是还没演过的第一场，才填 false。不要因为上一场的结尾看起来平常，就忽略笔记里没收尾的事。大纲里还没发生的钩子不算。
只输出 JSON：{"think": false, "why": "不超过 30 字"}"""

PLANNER_SYSTEM = """你在替故事主持人列这场的约束。她做什么、说什么由扮演她的人决定，你不能写。
- established：已经成立、这场必须保持一致的事。
- unresolved：还没了结、这场可能会碰到的事。碰不到也正常。
- consequences：要是碰到了，有哪些说得通的结果。好处和麻烦都写，不要只写顺利的一种。
不要安排新的戏剧性，不要写她的行动。每项不超过 120 字。
只输出 JSON：{"established": "", "unresolved": "", "consequences": ""}"""

JUDGE_SYSTEM = """你在比较同一故事起点下演出来的两场。甲和乙的顺序是随机的，不要因为哪边更长就判它更好。
只看写出来的情节（叙述和她的行动）。回答三个问题，取值只能是「甲」「乙」或「平」：
- reasonable：哪一场更合理。人物做事说得通，并且不和「这场之前」里已经发生的事矛盾。
- consequences：哪一场把这一步可能带来的后果想得更全，而不是只写眼前的一个反应。
- padding：哪一场更顺带加戏。无缘无故的新人物、新矛盾、抒情，算加戏。
平局就填「平」。只输出 JSON：{"reasonable": "平", "consequences": "平", "padding": "平"}"""


class SwitchingGM(DirectProvider):
    """按切法决定这一次调用开不开思考。``scene`` 恒开，``close`` 只在收场开。"""

    def __init__(self, base_url: str, api_key: str, model: str, mode: str) -> None:
        super().__init__(base_url, api_key, model, "gm", "off")
        self.mode = mode
        self.thinking_calls = 0

    async def text_chat(self, *, prompt: str, system_prompt: str = "", contexts=None, **_: Any):
        use = self.mode == "scene" or (self.mode == "close" and "任务：收场" in prompt)
        self.thinking = "on" if use else "off"
        if use:
            self.thinking_calls += 1
        async with SEM:
            return await super().text_chat(
                prompt=prompt, system_prompt=system_prompt, contexts=contexts
            )


class LimitedActor(DirectProvider):
    async def text_chat(self, **kwargs: Any):
        async with SEM:
            return await super().text_chat(**kwargs)


def _yes(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"true", "1", "yes", "是"}


def credentials() -> tuple[str, str]:
    base = os.environ.get("DSH_TEST_TEXT_BASE_URL", "").strip()
    key = os.environ.get("DSH_TEST_TEXT_API_KEY", "").strip()
    if not base or not key:
        raise SystemExit("需要 DSH_TEST_TEXT_BASE_URL / DSH_TEST_TEXT_API_KEY")
    return base, key


async def complete(
    base: str,
    key: str,
    model: str,
    system: str,
    user: str,
    *,
    thinking: str,
    temperature: float | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    payload: dict[str, Any] = {
        "model": model,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    if temperature is not None:
        payload["temperature"] = temperature
    if thinking in ("on", "off"):
        payload["thinking"] = {"type": "enabled" if thinking == "on" else "disabled"}
    started = time.perf_counter()
    async with SEM:
        result = await UrllibClient().post_json(
            chat_url(base), payload, {"Authorization": f"Bearer {key}"}, timeout_s=180.0
        )
    elapsed = time.perf_counter() - started
    usage = (result.body or {}).get("usage") if isinstance(result.body, dict) else {}
    details = (usage or {}).get("completion_tokens_details") or {}
    stat = {
        "seconds": round(elapsed, 2),
        "tokens_out": int((usage or {}).get("completion_tokens") or 0),
        "reasoning": int(details.get("reasoning_tokens") or 0),
        "ok": bool(result.ok),
    }
    if not result.ok or not isinstance(result.body, dict):
        return None, stat
    content = ((result.body.get("choices") or [{}])[0].get("message") or {}).get("content")
    parsed = extract_json(content)
    return (parsed if isinstance(parsed, dict) else None), stat


def render_scene(transcript: list[dict[str, Any]]) -> str:
    lines = []
    for turn in transcript:
        text = str(turn.get("text") or "").strip()
        if not text:
            continue
        who = "叙述" if turn.get("role") == "narrator" else "她"
        lines.append(f"{who}：{text}")
    return "\n".join(lines)


async def situation_brief(store: StoryStore, now: int) -> str:
    """判断和规划共用的外显材料：状态、最近梗概、笔记、上一场结尾。不含她的人格和内心。"""
    state = await store.state(BOT) or {}
    card = load_card(state.get("card"))
    card_line = (
        f"在{card.where}，正在{card.doing}" if card is not None else "（还没有状态卡，这是第一场）"
    )
    scenes = await store.recent_scenes(BOT, 5)
    scene_lines = [
        f"- {str(row.get('summary') or '').strip()}"
        for row in reversed(scenes)
        if str(row.get("summary") or "").strip()
    ]
    notes = await store.notes(BOT)
    note_lines = [f"- {row['title']}：{row['body']}" for row in notes]
    previous = await store.last_transcript(BOT)
    tail = ""
    if previous:
        start = max(
            (i for i, turn in enumerate(previous) if turn.get("role") == "me"),
            default=max(0, len(previous) - 1),
        )
        tail = render_scene(previous[start:])[:600]
    parts = [
        f"此刻：{clock_mod.iso(now)}。{card_line}。",
        "# 最近的经过\n" + ("\n".join(scene_lines) if scene_lines else "（还没有演过）"),
        "# 主持人笔记里还记着的事\n" + ("\n".join(note_lines) if note_lines else "（没有笔记）"),
    ]
    if tail:
        parts.append("# 上一场的结尾\n" + tail)
    return "\n\n".join(parts)


def plan_note(plan: dict[str, Any]) -> str:
    return "\n".join(
        [
            f"已经成立的：{str(plan.get('established') or '').strip()}",
            f"还没了结的：{str(plan.get('unresolved') or '').strip()}",
            f"可能的后果：{str(plan.get('consequences') or '').strip()}",
            "这是这场的约束，不是她的行动计划。不要把它原样写进叙述，也不要替她行动。",
        ]
    )


async def play(
    *,
    prefix: str,
    repeat: int,
    arm: str,
    mode: str,
    note: str,
    base: str,
    key: str,
    when: int,
    seed: int,
) -> dict[str, Any]:
    workdir = OUT / "_work" / f"{prefix}_{repeat}_{arm}"
    shutil.rmtree(workdir, ignore_errors=True)
    workdir.mkdir(parents=True, exist_ok=True)
    db_path = workdir / "memory.db"
    if prefix == "complex":
        shutil.copy(HISTORY, db_path)
    raw = {
        "story": {
            "enabled": True,
            "base_card": BASE_CARD,
            "narration": "first",
            "past_scenes": 0,
        },
        "text": {"base_url": base, "api_key": key, "model": ACTOR_MODEL, "max_tokens": 1200},
    }

    def configs(bot_id: str = "") -> ConfigView:
        return ConfigView(raw, bot_id)

    audit = Audit(logging.getLogger("thinking-switch"))
    db = Database(db_path)
    await db.connect()
    clock = Clock()
    clock.now = when
    try:
        stack = build_stack(
            db,
            configs,
            text=TextClient(configs(""), UrllibClient(), audit),
            jev=FakeJev(available=False),
        )
        store = StoryStore(db, now_fn=clock)
        if prefix == "plain":
            await store.replace_seed(BOT, validate_seed(world_seed()), clock.now)
            await seed_memory(db, clock.now)
            await store.update_state(
                BOT, clock.now, enabled=1, paused=0, failures=0,
                tokens=10.0, tokens_at=clock.now, next_wake_at=clock.now,
            )
        if note:
            await store.apply_notes(BOT, [{"title": "这场的约束", "body": note}], None, clock.now)
        gm = SwitchingGM(base, key, GM_MODEL, mode)
        actor = LimitedActor(base, key, ACTOR_MODEL, "她", "off")

        async def resolve(role: str, bot_id: str):
            return gm if role == "gm" else actor

        director = Director(
            db=db,
            configs=configs,
            store=store,
            memory=stack.service,
            llm=StoryLLM(resolve, audit),
            audit=audit,
            base_card=lambda bot_id: _base(),
            rng=random.Random(seed),
            now_fn=clock,
        )
        result = await director.run(BOT, manual=True)
        scene = await db.fetchone(
            "SELECT transcript, summary, rounds, calls FROM story_scenes WHERE id=?",
            (result.scene_id,),
        )
        return {
            "prefix": prefix,
            "repeat": repeat,
            "arm": arm,
            "ok": True,
            "rounds": scene["rounds"],
            "calls": scene["calls"],
            "summary": scene["summary"],
            "transcript": json.loads(scene["transcript"] or "[]"),
            "gm": {
                "requests": gm.requests,
                "tokens_out": gm.completion_tokens,
                "reasoning": gm.reasoning_tokens,
                "seconds": round(gm.seconds, 1),
                "thinking_calls": gm.thinking_calls,
            },
        }
    except Exception as exc:  # noqa: BLE001 - 一场失败不让整组停
        return {
            "prefix": prefix,
            "repeat": repeat,
            "arm": arm,
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}"[:300],
        }
    finally:
        await db.close()


async def _base() -> tuple[str, str]:
    return BASE_CARD, "thinking-switch"


async def judge_pair(
    prior: str, left: str, right: str, base: str, key: str
) -> dict[str, Any] | None:
    verdict, _stat = await complete(
        base,
        key,
        ACTOR_MODEL,
        JUDGE_SYSTEM,
        f"# 这场之前\n{prior}\n\n# 甲\n{left}\n\n# 乙\n{right}",
        thinking="off",
        temperature=0,
    )
    if verdict is None:
        return None
    cleaned = {}
    for axis in ("reasonable", "consequences", "padding"):
        value = str(verdict.get(axis) or "").strip()
        cleaned[axis] = value if value in ("甲", "乙", "平") else "平"
    return cleaned


def tally(pairs: list[dict[str, Any]], decisions: list[dict[str, Any]]) -> dict[str, Any]:
    fp = sum(1 for item in decisions if item["prefix"] == "plain" and item.get("think"))
    plain_n = sum(1 for item in decisions if item["prefix"] == "plain")
    report: dict[str, Any] = {}
    for arm in ("close", "scene", "plan"):
        mine = [pair for pair in pairs if pair["arm"] == arm and pair.get("verdict")]
        scores = {axis: {"arm": 0, "off": 0, "tie": 0} for axis in ("reasonable", "consequences", "padding")}
        for pair in mine:
            for axis, side in pair["verdict"].items():
                if side == "平":
                    scores[axis]["tie"] += 1
                elif side == pair["arm_side"]:
                    scores[axis]["arm"] += 1
                else:
                    scores[axis]["off"] += 1
        fp_ok = True if arm == "close" else fp <= FP_LIMIT
        passed = (
            len(mine) >= MIN_PAIRS
            and scores["reasonable"]["arm"] > scores["reasonable"]["off"]
            and scores["consequences"]["arm"] > scores["consequences"]["off"]
            and scores["padding"]["arm"] <= scores["padding"]["off"]
            and fp_ok
        )
        report[arm] = {
            "label": ARM_LABEL[arm],
            "pairs": len(mine),
            "scores": scores,
            "false_positives": None if arm == "close" else f"{fp}/{plain_n}",
            "passed": passed,
            "margin": scores["reasonable"]["arm"]
            - scores["reasonable"]["off"]
            + scores["consequences"]["arm"]
            - scores["consequences"]["off"],
        }
    return report


def pick_winner(
    report: dict[str, Any], scenes: list[dict[str, Any]], decisions: list[dict[str, Any]]
) -> str:
    passed = [arm for arm, row in report.items() if row["passed"]]
    if not passed:
        return ""

    def cost(arm: str) -> int:
        spent = sum(
            int((scene.get("gm") or {}).get("reasoning") or 0)
            for scene in scenes
            if scene.get("arm") == arm and scene.get("ok")
        )
        if arm == "plan":
            spent += sum(int((item.get("plan") or {}).get("reasoning") or 0) for item in decisions)
        return spent

    passed.sort(key=lambda arm: (-report[arm]["margin"], cost(arm)))
    return passed[0]


def print_report(report: dict[str, Any], scenes: list[dict[str, Any]], winner: str) -> None:
    print("\n===== 盲比（复杂起点，对「全程不开思考」）=====")
    for arm, row in report.items():
        scores = row["scores"]
        print(
            f"{row['label']}：合理 {scores['reasonable']['arm']} 胜 {scores['reasonable']['off']} 负 "
            f"{scores['reasonable']['tie']} 平；后果 {scores['consequences']['arm']} 胜 "
            f"{scores['consequences']['off']} 负 {scores['consequences']['tie']} 平；加戏 "
            f"{scores['padding']['arm']} 比对照更多 {scores['padding']['off']} 次对照更多；"
            f"平淡误开 {row['false_positives'] or '不适用'}；"
            f"{'达标' if row['passed'] else '未达标'}（{row['pairs']} 对）"
        )
    print("\n===== 花费（背景模型）=====")
    for arm in ARMS:
        mine = [scene["gm"] for scene in scenes if scene.get("arm") == arm and scene.get("ok")]
        if not mine:
            print(f"{ARM_LABEL.get(arm, arm)}：没有演成")
            continue
        n = len(mine)
        print(
            f"{ARM_LABEL.get(arm, arm)}：{n} 场，平均 {sum(g['requests'] for g in mine) / n:.1f} 次请求，"
            f"输出 {sum(g['tokens_out'] for g in mine) // n} token（思考 "
            f"{sum(g['reasoning'] for g in mine) // n}），{sum(g['seconds'] for g in mine) / n:.1f} 秒，"
            f"其中开了思考的调用平均 {sum(g['thinking_calls'] for g in mine) / n:.1f} 次"
        )
    failed = [scene for scene in scenes if not scene.get("ok")]
    if failed:
        print(f"\n没演成 {len(failed)} 场")
        for scene in failed[:8]:
            print(f"  {scene['prefix']} #{scene['repeat']} {scene['arm']}：{scene.get('error')}")
    print("\n获胜：" + (ARM_LABEL[winner] if winner else "无，维持全程不开思考"))


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repeats", type=int, default=6)
    parser.add_argument("--history", default=str(HISTORY))
    args = parser.parse_args()
    if args.repeats > len(PLAIN_STARTS):
        raise SystemExit(f"重复次数最多 {len(PLAIN_STARTS)}")
    history = Path(args.history)
    if not history.is_file():
        raise SystemExit(f"找不到复杂起点 {history}，先生成 30 场历史")
    base, key = credentials()
    logging.basicConfig(level=logging.ERROR)
    OUT.mkdir(parents=True, exist_ok=True)

    # 同一次重复的判断，B 和 C 共用。平淡起点没有经过，复杂起点读历史库（只读）。
    decisions: list[dict[str, Any]] = []
    plans: dict[tuple[str, int], str] = {}
    priors: dict[tuple[str, int], str] = {}
    jobs: list[dict[str, Any]] = []
    for prefix in ("plain", "complex"):
        for repeat in range(args.repeats):
            if prefix == "plain":
                when = int(datetime.fromisoformat(PLAIN_STARTS[repeat]).timestamp() * 1000)
                brief = await _plain_brief(when)
            else:
                when, brief = await _complex_brief(history, repeat)
            priors[(prefix, repeat)] = brief
            verdict, stat = await complete(
                base, key, GM_MODEL, DETECTOR_SYSTEM, brief, thinking="off", temperature=0
            )
            think = _yes((verdict or {}).get("think"))
            decisions.append(
                {
                    "prefix": prefix,
                    "repeat": repeat,
                    "think": think,
                    "why": str((verdict or {}).get("why") or "")[:80],
                    **stat,
                }
            )
            note = ""
            if think:
                plan, plan_stat = await complete(
                    base, key, GM_MODEL, PLANNER_SYSTEM, brief, thinking="on"
                )
                if plan:
                    note = plan_note(plan)
                    plans[(prefix, repeat)] = note
                decisions[-1]["plan"] = plan_stat
            seed = (1000 if prefix == "plain" else 2000) + repeat
            for arm in ARMS:
                mode = {"off": "off", "close": "close", "scene": "scene" if think else "off", "plan": "off"}[arm]
                jobs.append(
                    {
                        "prefix": prefix,
                        "repeat": repeat,
                        "arm": arm,
                        "mode": mode,
                        "note": note if arm == "plan" else "",
                        "when": when,
                        "seed": seed,
                    }
                )
            print(
                f"{prefix} #{repeat} 判断={'要多想' if think else '不多想'} "
                f"{decisions[-1]['why']}"
            )

    scenes = await asyncio.gather(
        *[
            play(base=base, key=key, **{k: job[k] for k in ("prefix", "repeat", "arm", "mode", "note", "when", "seed")})
            for job in jobs
        ]
    )
    by_key = {(s["prefix"], s["repeat"], s["arm"]): s for s in scenes}

    pairs: list[dict[str, Any]] = []
    for repeat in range(args.repeats):
        control = by_key.get(("complex", repeat, "off"))
        if not control or not control.get("ok"):
            continue
        prior = priors[("complex", repeat)]
        for index, arm in enumerate(("close", "scene", "plan")):
            other = by_key.get(("complex", repeat, arm))
            if not other or not other.get("ok"):
                continue
            swap = random.Random(8000 + repeat * 10 + index).random() < 0.5
            left_arm, right_arm = (arm, "off") if swap else ("off", arm)
            left = render_scene(by_key[("complex", repeat, left_arm)]["transcript"])
            right = render_scene(by_key[("complex", repeat, right_arm)]["transcript"])
            verdict = await judge_pair(prior, left, right, base, key)
            pairs.append(
                {
                    "repeat": repeat,
                    "arm": arm,
                    "arm_side": "甲" if left_arm == arm else "乙",
                    "verdict": verdict,
                }
            )

    report = tally(pairs, decisions)
    winner = pick_winner(report, list(scenes), decisions)
    saved = {
        "decisions": decisions,
        "scenes": [
            {k: v for k, v in scene.items() if k != "transcript"} | {
                "text": render_scene(scene["transcript"]) if scene.get("ok") else ""
            }
            for scene in scenes
        ],
        "pairs": pairs,
        "report": report,
        "winner": winner,
        "plans": {f"{prefix}_{repeat}": text for (prefix, repeat), text in plans.items()},
    }
    (OUT / "results.json").write_text(
        json.dumps(saved, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    print_report(report, list(scenes), winner)
    return 0


async def _plain_brief(when: int) -> str:
    """平淡起点还没有库：用一次临时库导入种子，取出和正式一场相同的材料。"""
    workdir = OUT / "_brief"
    shutil.rmtree(workdir, ignore_errors=True)
    workdir.mkdir(parents=True, exist_ok=True)
    db = Database(workdir / "memory.db")
    await db.connect()
    clock = Clock()
    clock.now = when
    try:
        store = StoryStore(db, now_fn=clock)
        await store.replace_seed(BOT, validate_seed(world_seed()), when)
        return await situation_brief(store, when)
    finally:
        await db.close()
        shutil.rmtree(workdir, ignore_errors=True)


async def _complex_brief(history: Path, repeat: int) -> tuple[int, str]:
    db = Database(history)
    await db.connect()
    try:
        store = StoryStore(db)
        state = await store.state(BOT)
        if state is None or not str(state.get("outline") or "").strip():
            raise SystemExit("历史库里没有这个机器人的故事")
        last = int(state.get("last_scene_at") or 0)
        when = last + (20 + repeat * 15) * 60_000
        return when, await situation_brief(store, when)
    finally:
        await db.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
