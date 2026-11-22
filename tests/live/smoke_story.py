"""故事模块的真实模型冒烟（手动运行，不进 CI）。

用法（在工作区根目录）：
    $env:DSH_TEST_TEXT_BASE_URL="https://api.deepseek.com/v1"
    $env:DSH_TEST_TEXT_API_KEY="sk-..."
    $env:DSH_TEST_TEXT_MODEL="deepseek-chat"
    .venv\\Scripts\\python.exe -X utf8 astrbot_plugin_shadow_memory/tests/live/smoke_story.py --scenes 3

只读环境变量，不读 .env，不打印密钥。背景模型、场景中的她、记忆整理、显著度判断都用同一个
OpenAI 兼容站点；数据写在 ``.pytest_tmp/live_story/`` 下的临时库里，不碰真实数据。

它用真实模型连着演几场（每场之后把时钟拨到背景模型预约的时间），打印每一场她看到的经过、
骰子、状态卡、新设定、笔记和她记下的记忆，最后给出调用次数、重试次数和 token 用量。
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
from types import SimpleNamespace
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from astrbot_plugin_shadow_memory.core import clock as clock_mod  # noqa: E402
from astrbot_plugin_shadow_memory.core.audit import Audit  # noqa: E402
from astrbot_plugin_shadow_memory.core.config import ConfigView  # noqa: E402
from astrbot_plugin_shadow_memory.core.db import Database  # noqa: E402
from astrbot_plugin_shadow_memory.core.decision import JevClient, chat_url  # noqa: E402
from astrbot_plugin_shadow_memory.core.textgen import TextClient  # noqa: E402
from astrbot_plugin_shadow_memory.interaction.environment import (  # noqa: E402
    build_environment_prompt,
)
from astrbot_plugin_shadow_memory.story import prompts as story_prompts  # noqa: E402
from astrbot_plugin_shadow_memory.story.director import Director  # noqa: E402
from astrbot_plugin_shadow_memory.story.llm import StoryLLM  # noqa: E402
from astrbot_plugin_shadow_memory.story.service import StoryService  # noqa: E402
from astrbot_plugin_shadow_memory.story.store import StoryStore  # noqa: E402
from astrbot_plugin_shadow_memory.tests.live.smoke_models import UrllibClient  # noqa: E402
from astrbot_plugin_shadow_memory.tests.unit.helpers import build_stack  # noqa: E402

BOT = "live-bot"
BASE_CARD = (
    "你是小影，青屿大学中文系大三的学生，住在学校北门外的合租屋里。"
    "你话不多，慢热，熟了以后会冷不丁开玩笑；怕麻烦别人，遇到冲突习惯先躲开。"
    "你喜欢下雨天、便利店的关东煮和旧书，不喜欢被催。说话口语化，句子短。"
)
PERSONA = [
    ("core", "我遇到让我为难的请求时，会先含糊地应一声拖着，等自己想清楚了再给准话，不喜欢被当场逼着表态。"),
    ("edge", "和不熟的人单独相处时，我会找点手上的事来做，比如整理东西或者看手机，免得两个人干坐着没话说。"),
    ("tension", "阿澄欠我的两百块一直没提，我不知道是她忘了还是装不记得，也不知道该不该开口要。"),
]


class DirectProvider:
    """最小的 OpenAI 兼容对话 provider：只实现故事模块用到的 ``text_chat``。

    不设 ``response_format``，也不改温度：AstrBot 的 provider 默认也不会替插件加这些，
    这样测出来的 JSON 遵从率才接近真实情况。
    """

    def __init__(self, base_url: str, api_key: str, model: str, label: str) -> None:
        self.url = chat_url(base_url)
        self.api_key = api_key
        self.model = model
        self.label = label
        self.http = UrllibClient()
        self.requests = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.cached_tokens = 0
        self.seconds = 0.0

    async def text_chat(self, *, prompt: str, system_prompt: str = "", contexts=None, **_: Any):
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})
        started = time.perf_counter()
        result = await self.http.post_json(
            self.url,
            {"model": self.model, "messages": messages},
            {"Authorization": f"Bearer {self.api_key}"},
            timeout_s=120.0,
        )
        self.seconds += time.perf_counter() - started
        self.requests += 1
        if not result.ok or not isinstance(result.body, dict):
            raise RuntimeError(
                f"HTTP {result.status} {result.error or str(result.text)[:200]}"
            )
        usage = result.body.get("usage") or {}
        self.prompt_tokens += int(usage.get("prompt_tokens") or 0)
        self.completion_tokens += int(usage.get("completion_tokens") or 0)
        self.cached_tokens += int(usage.get("prompt_cache_hit_tokens") or 0)
        message = (result.body.get("choices") or [{}])[0].get("message") or {}
        return SimpleNamespace(completion_text=message.get("content") or "")


class Clock:
    def __init__(self) -> None:
        self.now = clock_mod.now_ms()

    def __call__(self) -> int:
        return self.now


def world_seed() -> dict[str, Any]:
    path = ROOT / "astrbot_plugin_shadow_memory" / "docs" / "world_seed.example.json"
    return json.loads(path.read_text(encoding="utf-8"))


async def seed_memory(db: Database, now: int) -> None:
    day = clock_mod.day_index(now)
    for layer, text in PERSONA:
        await db.execute(
            "INSERT INTO persona_entries(bot_id, layer, text, source_event_ids, created_day, "
            "last_edited_day, active) VALUES(?,?,?,?,?,?,1)",
            (BOT, layer, text, "[]", day, day),
        )
    # 一个聊天里认识的真人和一条聊天记忆：用来看背景模型会不会把“他说寄了”当成
    # “已经寄到”，以及会不会在场景里替他说话（会被拦下并重试，日志里有 WARN）。
    await db.execute(
        "INSERT INTO people(bot_id, person_id, name, aliases, last_topic, last_seen, "
        "created_at, updated_at) VALUES(?,?,?,?,?,?,?,?)",
        (BOT, "u1", "小明", "[]", "寄茶", now - 3_600_000, now, now),
    )
    await db.execute(
        "INSERT INTO events(bot_id, umo, kind, started_at, ended_at, fragment, entities, "
        "storage_strength, retrievability, last_recalled_at, salience, status, source, created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (BOT, "aiocqhttp:FriendMessage:u1", "episodic", now - 3_600_000, now - 3_600_000,
         "小明说给我寄了一盒茶，过几天到", '["小明", "茶", "快递"]', 8.75, 1.0,
         now - 3_600_000, 3, "closed", "chat", now - 3_600_000),
    )


def show(title: str, body: str = "") -> None:
    print(f"\n--- {title} ---")
    if body:
        print(body)


async def print_scene(db: Database, store: StoryStore, index: int, clock: Clock) -> bool:
    scene = await db.fetchone("SELECT * FROM story_scenes ORDER BY id DESC LIMIT 1")
    if scene is None:
        print("（没有场景记录）")
        return False
    print(f"\n==================== 第 {index} 场 · {clock_mod.iso(int(scene['started_at']))} ====================")
    seed = json.loads(scene["seed"] or "{}")
    print(f"偶然种子：{seed.get('focus')} / {'、'.join(seed.get('words') or [])}")
    if scene["status"] != "ok":
        print(f"!! 没演成：{scene['error']}（走到第 {scene['rounds']} 轮，{scene['calls']} 次调用）")
        transcript = json.loads(scene["transcript"] or "[]")
        if transcript:
            show("失败前的经过", story_prompts.render_transcript(transcript, viewer="me"))
        return False
    if scene["backfill"]:
        show("补叙", scene["backfill"])
    transcript = json.loads(scene["transcript"] or "[]")
    show("经过（她的视角，含内心）", story_prompts.render_transcript(transcript, viewer="me"))
    rolls = json.loads(scene["rolls"] or "[]")
    for roll in rolls:
        print(
            f"  骰子 第{roll['round']}轮「{roll['what']}」：申报 {roll['likelihood']}"
            f"（{roll['chance']:.0%}），掷出 {roll['value']:.2f} → {roll['label']}"
        )
    show("背景模型的梗概（上帝视角）", scene["summary"] or "（空）")
    event = await db.fetchone("SELECT * FROM events WHERE id=?", (scene["event_id"],))
    show(
        "她记下的",
        f"碎片：{event['fragment']}\n摘要：{event['summary']}\n实体：{event['entities']}\n"
        f"显著度：{event['salience']}  S0：{event['storage_strength']:.2f}",
    )
    if scene["backfill_event_id"]:
        backfill = await db.fetchone(
            "SELECT fragment, salience FROM events WHERE id=?", (scene["backfill_event_id"],)
        )
        print(f"补叙记忆（显著度 {backfill['salience']}）：{backfill['fragment']}")
    state = await store.state(BOT)
    card = json.loads(state["card"])
    wake = (int(state["next_wake_at"]) - clock.now) / 60_000
    show(
        "收场",
        f"状态卡：在 {card['where']} / {card['doing']} / 到 {clock_mod.iso(card['until'])[11:16]}"
        f" / 看手机{'方便' if card['phone_ok'] else '不方便'}\n"
        f"下次唤醒：{wake:.0f} 分钟后   剩余额度：{state['tokens']:.2f}\n"
        f"本场：{scene['rounds']} 轮，{scene['calls']} 次调用",
    )
    canon = await db.fetchall(
        "SELECT category, name, body FROM story_canon WHERE bot_id=? AND scene_id=?",
        (BOT, scene["id"]),
    )
    if canon:
        show("新登记的设定", "\n".join(f"[{row['category']}] {row['name']}：{row['body']}" for row in canon))
    notes = await db.fetchall(
        "SELECT title, body FROM story_notes WHERE bot_id=? AND scene_id=?", (BOT, scene["id"])
    )
    if notes:
        show("本场写的主持人笔记", "\n".join(f"《{row['title']}》{row['body']}" for row in notes))
    return True


async def chat_probe(provider: DirectProvider, service: StoryService, stack: Any, clock: Clock) -> None:
    """状态卡注入聊天后，她知不知道自己在哪；被问到没发生的事时会不会编结果。"""
    lines = await service.chat_lines(BOT, clock.now)
    environment = build_environment_prompt(
        platform="aiocqhttp",
        chat_type="private",
        sender_name="小明",
        rules="- 直接用一两句话回复对方，不要解释。",
        life_lines=lines,
    )
    system = "\n\n".join([BASE_CARD, await stack.persona.render(BOT), environment])
    show("聊天侧注入的环境块", environment)
    for question in ("在干嘛呢", "对了，阿澄把那两百块还你了吗？"):
        reply = await provider.text_chat(prompt=f"小明: {question}", system_prompt=system)
        print(f"\n小明：{question}\n她：{reply.completion_text.strip()}")


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenes", type=int, default=3)
    parser.add_argument("--rounds", type=int, default=4)
    parser.add_argument("--seed", type=int, default=None, help="随机种子（骰子与偶然种子）")
    parser.add_argument("--no-chat", action="store_true")
    parser.add_argument(
        "--narration", default="second", choices=["second", "first", "none"],
        help="背景模型的叙述人称",
    )
    parser.add_argument("--start", default="", help="起始时间，如 2026-11-23T08:00；默认现在")
    args = parser.parse_args()

    base = os.environ.get("DSH_TEST_TEXT_BASE_URL", "").strip()
    key = os.environ.get("DSH_TEST_TEXT_API_KEY", "").strip()
    model = os.environ.get("DSH_TEST_TEXT_MODEL", "deepseek-chat").strip() or "deepseek-chat"
    if not base or not key:
        print("需要环境变量 DSH_TEST_TEXT_BASE_URL 和 DSH_TEST_TEXT_API_KEY")
        return 2

    logging.basicConfig(level=logging.WARNING, format="[%(levelname)s] %(message)s", stream=sys.stdout)
    workdir = ROOT / ".pytest_tmp" / "live_story"
    shutil.rmtree(workdir, ignore_errors=True)
    workdir.mkdir(parents=True, exist_ok=True)

    raw_config = {
        "story": {
            "enabled": True, "base_card": BASE_CARD, "max_rounds": args.rounds,
            "narration": args.narration,
        },
        "text": {"base_url": base, "api_key": key, "model": model, "max_tokens": 1200},
        "decision": {
            "base_url": base, "api_key": key, "model": model,
            "protocol": "chat", "timeout_ms": 15000,
        },
    }

    def configs(bot_id: str = "") -> ConfigView:
        return ConfigView(raw_config, bot_id)

    audit = Audit(logging.getLogger("story-live"))
    http = UrllibClient()
    db = Database(workdir / "memory.db")
    await db.connect()
    clock = Clock()
    if args.start:
        clock.now = int(datetime.fromisoformat(args.start).timestamp() * 1000)
    try:
        stack = build_stack(
            db,
            configs,
            text=TextClient(configs(""), http, audit),
            jev=JevClient(configs(""), http, audit),
        )
        await seed_memory(db, clock.now)
        providers = {
            "gm": DirectProvider(base, key, model, "背景模型"),
            "actor": DirectProvider(base, key, model, "场景中的她"),
        }

        async def resolve_provider(role: str, bot_id: str) -> DirectProvider:
            return providers[role]

        async def resolve_base(bot_id: str) -> tuple[str, str]:
            return BASE_CARD, "smoke"

        llm = StoryLLM(resolve_provider, audit)
        store = StoryStore(db, now_fn=clock)
        director = Director(
            db=db, configs=configs, store=store, memory=stack.service, llm=llm, audit=audit,
            base_card=resolve_base, rng=random.Random(args.seed), now_fn=clock,
        )
        service = StoryService(
            configs=configs, store=store, director=director, llm=llm, audit=audit,
            base_card=resolve_base, now_fn=clock,
        )
        await service.import_seed(BOT, world_seed())
        await service.enable(BOT)
        print(
            f"模型：{model}   轮数上限：{args.rounds}   叙述人称：{args.narration}   "
            f"起始时间：{clock_mod.iso(clock.now)}"
        )

        played = 0
        for index in range(1, args.scenes + 1):
            started = time.perf_counter()
            ran = await service.tick()
            elapsed = time.perf_counter() - started
            ok = await print_scene(db, store, index, clock)
            print(f"（这一场真实耗时 {elapsed:.0f} 秒）")
            played += 1 if ran and ok else 0
            state = await store.state(BOT)
            if int(state["paused"] or 0):
                print("故事已自动暂停，停止。")
                break
            # 把时钟拨到预约的唤醒时间，模拟现实时间流逝
            clock.now = max(clock.now + 60_000, int(state["next_wake_at"] or clock.now))

        if not args.no_chat and played:
            state = await store.state(BOT)
            card = json.loads(state["card"])
            clock.now = int(card["updated_at"]) + 5 * 60_000  # 上一场结束后五分钟来聊天
            await chat_probe(providers["actor"], service, stack, clock)

        show("统计")
        print(f"演成 {played} / {args.scenes} 场")
        print(f"故事模型调用 {llm.stats['calls']} 次，其中没给出合法输出而放弃 {llm.stats['failures']} 次")
        for role, provider in providers.items():
            print(
                f"{provider.label}：{provider.requests} 次请求，输入 {provider.prompt_tokens} token"
                f"（缓存命中 {provider.cached_tokens}），输出 {provider.completion_tokens} token，"
                f"共 {provider.seconds:.0f} 秒"
            )
        print(f"写作模型：{getattr(stack.text, 'stats', {})}")
        print(f"判断模型：{getattr(stack.jev, 'stats', {})}")
        return 0
    finally:
        await db.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
