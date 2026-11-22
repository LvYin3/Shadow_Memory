"""故事模块测试用的替身与装配。"""

from __future__ import annotations

import random
from datetime import datetime
from types import SimpleNamespace
from typing import Any

from astrbot_plugin_shadow_memory.story.director import Director
from astrbot_plugin_shadow_memory.story.llm import StoryLLMError
from astrbot_plugin_shadow_memory.story.service import StoryService
from astrbot_plugin_shadow_memory.story.store import StoryStore

from .helpers import build_stack

MINUTE = 60_000
HOUR = 3_600_000
DAY = 86_400_000
NOW = int(datetime(2026, 11, 22, 14, 0).timestamp() * 1000)
BOT = "bot1"

BASE_CARD = "她叫小影，青屿大学大三学生，话不多。"
STORY_ON = {"story": {"enabled": True}}

OPENING = {"backfill": "", "narration": "你站在便利店门口，雨下大了。", "lookup": []}
ACTION = {"inner": "其实不想回去", "action": "我走进便利店，想买把伞。"}
PLAIN_TURN = {"check": None, "narration": "店里只剩一把黑伞，你买下了它。", "lookup": [], "end": True}
CLOSING = {
    "epilogue": "",
    "card": {"where": "便利店", "doing": "躲雨", "minutes": 40, "phone": "方便"},
    "next_wake_minutes": 45,
    "canon": [{"name": "便利店老板", "category": "人物", "body": "姓周，话少。"}],
    "notes": [{"title": "暗线", "body": "室友阿澄在瞒着她找工作"}],
    "summary": "她在便利店躲雨，买到最后一把伞。",
}
MEMORY = {
    "fragment": "我在便利店躲雨，买到了最后一把黑伞",
    "entities": ["便利店", "伞", "雨"],
    "summary": "下雨了，我进便利店买了伞。",
    "backfill_fragment": "",
}


def world_seed() -> dict[str, Any]:
    return {
        "version": "world_seed/v1",
        "title": "青屿",
        "outline": "她是青屿大学的大三学生，住在学校北门外的合租屋里，日子过得平常。" * 2,
        "entries": [
            {"category": "地点", "name": "青屿大学", "body": "一所靠海的综合性大学。"},
            {"category": "人物", "name": "阿澄", "body": "她的室友，学美术，作息混乱。"},
        ],
        "hooks": ["期末前要交一份小组报告"],
        "opening": {"where": "合租屋", "doing": "赖床"},
    }


class Clock:
    """可手动拨动的时钟。"""

    def __init__(self, now: int = NOW) -> None:
        self.now = now

    def __call__(self) -> int:
        return self.now

    def advance(self, ms: int) -> None:
        self.now += ms


class ScriptedRng:
    """裁定骰按给定序列出数（用完后一律 0.99）；抽偶然种子用固定种子。"""

    def __init__(self, values: list[float] | None = None) -> None:
        self.values = list(values or [])
        self._inner = random.Random(1)

    def random(self) -> float:
        return self.values.pop(0) if self.values else 0.99

    def sample(self, population, k):
        return self._inner.sample(population, k)

    def choice(self, seq):
        return self._inner.choice(seq)


class FakeStoryLLM:
    """按角色排队出结果的模型替身；队列空了就当模型失败。"""

    def __init__(self) -> None:
        self.queues: dict[str, list[Any]] = {"gm": [], "actor": []}
        self.calls: list[tuple[str, str, str]] = []
        self.unavailable: set[str] = set()

    def push(self, role: str, *values: Any) -> None:
        self.queues[role].extend(values)

    def prompts(self, role: str) -> list[str]:
        return [user for called, _, user in self.calls if called == role]

    def systems(self, role: str) -> list[str]:
        return [system for called, system, _ in self.calls if called == role]

    async def available(self, role: str, bot_id: str) -> bool:
        return role not in self.unavailable

    async def ask_json(self, role, bot_id, system, user, *, validate=None):
        self.calls.append((role, system, user))
        queue = self.queues[role]
        if not queue:
            raise StoryLLMError(f"{role} 没有给出合法输出", 2)
        value = queue.pop(0)
        if isinstance(value, Exception):
            raise value
        verdict = validate(value) if validate is not None else True
        if verdict is not True:
            reason = verdict if isinstance(verdict, str) else "输出缺少必填内容"
            raise StoryLLMError(f"{role} {reason}", 2)
        return value, 1


def build_story(
    db,
    configs,
    *,
    rng: Any = None,
    clock: Clock | None = None,
    text: Any = None,
    jev: Any = None,
    base_card: tuple[str, str] = (BASE_CARD, "story.base_card"),
) -> SimpleNamespace:
    stack = build_stack(db, configs, text=text, jev=jev)
    clock = clock or Clock()
    llm = FakeStoryLLM()
    store = StoryStore(db, now_fn=clock)
    holder = {"card": base_card}

    async def resolve_base(bot_id: str) -> tuple[str, str]:
        return holder["card"]

    director = Director(
        db=db,
        configs=configs,
        store=store,
        memory=stack.service,
        llm=llm,
        audit=stack.audit,
        base_card=resolve_base,
        rng=rng or ScriptedRng(),
        now_fn=clock,
    )
    service = StoryService(
        configs=configs,
        store=store,
        director=director,
        llm=llm,
        audit=stack.audit,
        base_card=resolve_base,
        now_fn=clock,
    )
    return SimpleNamespace(
        stack=stack,
        db=db,
        clock=clock,
        llm=llm,
        store=store,
        director=director,
        service=service,
        base=holder,
    )


def script_scene(
    env: SimpleNamespace,
    *,
    opening: dict[str, Any] | None = None,
    turns: list[dict[str, Any]] | None = None,
    closing: dict[str, Any] | None = None,
    memory: dict[str, Any] | None = MEMORY,
) -> None:
    """排好一整场的模型输出：开场、每轮（她的行动 + 背景模型的裁定）、收场、记忆整理。"""
    env.llm.push("gm", dict(opening or OPENING))
    for turn in turns or [PLAIN_TURN]:
        env.llm.push("actor", dict(ACTION))
        env.llm.push("gm", dict(turn))
    env.llm.push("gm", dict(closing or CLOSING))
    if memory is not None:
        env.stack.text.push(dict(memory))


async def start_story(env: SimpleNamespace, bot_id: str = BOT) -> None:
    await env.service.import_seed(bot_id, world_seed())
    await env.service.enable(bot_id)
