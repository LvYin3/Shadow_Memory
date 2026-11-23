"""故事模块对照实测（手动运行，不进 CI）：几组配置各演若干条时间线，再让模型逐条审越界。

用法（工作区根目录，密钥只从环境变量读，不读 .env）：
    $env:DSH_TEST_TEXT_BASE_URL="https://api.deepseek.com/v1"
    $env:DSH_TEST_TEXT_API_KEY="sk-..."
    .venv\\Scripts\\python.exe -X utf8 astrbot_plugin_shadow_memory/tests/live/ab_story.py `
        --tag narration --arm "second:narration=second" --arm "first:narration=first"

每个 ``--arm`` 是 ``名字:键=值,键=值``，可用的键：
- ``narration``：second / first / none（默认 first）
- ``gm`` / ``actor``：背景模型 / 她用的模型（默认 DSH_TEST_TEXT_MODEL）
- ``thinking``：背景模型的思考开关 default / on / off（默认 default，按模型自己的默认）
- ``past``：开场最多带几条更早的场景（默认 3，0 关掉）

设了 ``DSH_TEST_JEV_BASE_URL`` / ``DSH_TEST_JEV_API_KEY`` / ``DSH_TEST_JEV_MODEL`` 时用真实判断模型
（显著度、她的召回、背景模型回想旧场景），否则判断模型视为不可用。审稿模型默认
``DSH_TEST_JUDGE_MODEL``，没设就用 deepseek-chat。结果写在 ``.pytest_tmp/ab/<tag>/``，不含密钥。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import random
import re
import shutil
import statistics
import sys
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from astrbot_plugin_shadow_memory.core.audit import Audit  # noqa: E402
from astrbot_plugin_shadow_memory.core.config import ConfigView  # noqa: E402
from astrbot_plugin_shadow_memory.core.db import Database  # noqa: E402
from astrbot_plugin_shadow_memory.core.decision import JevClient, chat_url  # noqa: E402
from astrbot_plugin_shadow_memory.core.jsonx import extract_json  # noqa: E402
from astrbot_plugin_shadow_memory.core.textgen import TextClient  # noqa: E402
from astrbot_plugin_shadow_memory.story.director import Director  # noqa: E402
from astrbot_plugin_shadow_memory.story.llm import StoryLLM  # noqa: E402
from astrbot_plugin_shadow_memory.story.service import StoryService  # noqa: E402
from astrbot_plugin_shadow_memory.story.store import StoryStore  # noqa: E402
from astrbot_plugin_shadow_memory.tests.fakes import FakeJev  # noqa: E402
from astrbot_plugin_shadow_memory.tests.live.smoke_models import UrllibClient  # noqa: E402
from astrbot_plugin_shadow_memory.tests.live.smoke_story import (  # noqa: E402
    BASE_CARD,
    BOT,
    Clock,
    DirectProvider,
    decision_settings,
    seed_memory,
    world_seed,
)
from astrbot_plugin_shadow_memory.tests.unit.helpers import build_stack  # noqa: E402

STARTS = ["2026-11-23T07:30", "2026-11-23T12:10", "2026-11-23T17:40", "2026-11-23T21:30"]
OUT = ROOT / ".pytest_tmp" / "ab"
SEM = asyncio.Semaphore(8)

JUDGE_SYSTEM = """你在审核一段“主持人—玩家”叙事记录。主角是一个女生。
- N 开头的是主持人写的叙述。主持人只应该写：主角看到、听到、闻到、身体感觉到的东西；别人说的话和做的事；以及主角已经做出的行动带来的结果。叙述可能用“你”、用“我”，或者不带人称来指主角，这三种都正常，不要因为人称不同而区别对待。
- A 开头的是主角自己写的行动。主角只应该写自己做了什么、说了什么。

逐条判断。
对每条 N：
- interior：主持人是否写出了主角的内心——想法、情绪、判断、打算、决定、犹豫（例：“觉得有点烦”“不知道该不该去”“决定不回了”“心里一松”“懒得去捡”）。单纯的身体感觉（冷、困、饿、疼、烫）和感官所见所闻不算。
- acts：主持人是否让主角做了她自己没写过的新的主动行为，或说了她没说过的话（例：她只写“走进便利店”，叙述却写她“挑了一把伞并付了钱”）。复述她在前一条 A 里写过的行动、写这些行动的直接结果、以及第一条 N 里交代她原本正在做的事，都不算。
对每条 A：
- outcome：主角是否写了不该由她决定的事——别人的反应或回答、事情成没成、环境里新发生的事（例：“我问老板，他说可以便宜五块”）。只写自己的动作、自己说的话、自己的打算不算。

每个 true 都给出最能说明问题的原文片段（不超过 30 字）；false 就给空字符串。
只输出 JSON：{"N": [{"id": "N1", "interior": false, "interior_quote": "", "acts": false, "acts_quote": ""}], "A": [{"id": "A1", "outcome": false, "quote": ""}]}"""

QUOTED = re.compile(r"“[^”]*”|「[^」]*」|\"[^\"]*\"|『[^』]*』")


@dataclass(frozen=True, slots=True)
class Arm:
    name: str
    narration: str = "first"
    gm: str = ""
    actor: str = ""
    thinking: str = "default"
    past: int = 3

    @classmethod
    def parse(cls, spec: str) -> "Arm":
        name, _, rest = spec.partition(":")
        fields: dict[str, Any] = {}
        for pair in filter(None, rest.split(",")):
            key, _, value = pair.partition("=")
            key, value = key.strip(), value.strip()
            if key == "past":
                fields[key] = int(value)
            elif key in {"narration", "gm", "actor", "thinking"}:
                fields[key] = value
            else:
                raise SystemExit(f"不认识的键：{key}")
        return cls(name=name.strip(), **fields)


class Limited(DirectProvider):
    async def text_chat(self, **kwargs: Any):
        async with SEM:
            return await super().text_chat(**kwargs)


class CountingAudit(Audit):
    def __init__(self) -> None:
        super().__init__(logging.getLogger("ab"), debug=True)
        self.rejected = 0
        self.guard_person = 0
        self.guard_speech = 0
        self.scenes: list[dict[str, Any]] = []
        self.past: list[dict[str, Any]] = []

    def warn(self, message: str, *args: Any) -> None:
        text = message % args if args else message
        if "不是 JSON" in text or "不合格" in text:
            self.rejected += 1
        if "现实里的人" in text:
            self.guard_person += 1
        if "替她说了话" in text:
            self.guard_speech += 1

    def event(self, kind: str, **fields: Any) -> None:
        if kind == "story_scene":
            self.scenes.append(fields)

    def debug(self, kind: str, **fields: Any) -> None:
        if kind == "story_past":
            self.past.append(fields)


async def run_stream(
    arm: Arm, index: int, scenes: int, base: str, key: str, model: str, real_jev: bool
) -> dict[str, Any]:
    workdir = OUT / "_work" / f"{arm.name}_{index}"
    shutil.rmtree(workdir, ignore_errors=True)
    workdir.mkdir(parents=True, exist_ok=True)
    raw = {
        "story": {
            "enabled": True, "base_card": BASE_CARD, "narration": arm.narration,
            "past_scenes": arm.past,
        },
        "text": {"base_url": base, "api_key": key, "model": model, "max_tokens": 1200},
        "decision": decision_settings(base, key, model),
    }

    def configs(bot_id: str = "") -> ConfigView:
        return ConfigView(raw, bot_id)

    audit = CountingAudit()
    db = Database(workdir / "memory.db")
    await db.connect()
    clock = Clock()
    clock.now = int(datetime.fromisoformat(STARTS[index % len(STARTS)]).timestamp() * 1000)
    try:
        jev = JevClient(configs(""), UrllibClient(), audit) if real_jev else FakeJev(available=False)
        stack = build_stack(
            db, configs, text=TextClient(configs(""), UrllibClient(), audit), jev=jev
        )
        await seed_memory(db, clock.now)
        providers = {
            "gm": Limited(base, key, arm.gm or model, "gm", arm.thinking),
            "actor": Limited(base, key, arm.actor or model, "actor"),
        }

        async def resolve(role: str, bot_id: str):
            return providers[role]

        async def base_card(bot_id: str):
            return BASE_CARD, "ab"

        llm = StoryLLM(resolve, audit)
        store = StoryStore(db, now_fn=clock)
        director = Director(
            db=db, configs=configs, store=store, memory=stack.service, llm=llm, audit=audit,
            base_card=base_card, rng=random.Random(100 + index), now_fn=clock,
        )
        service = StoryService(
            configs=configs, store=store, director=director, llm=llm, audit=audit,
            base_card=base_card, now_fn=clock,
        )
        await service.import_seed(BOT, world_seed())
        await service.enable(BOT)
        for _ in range(scenes):
            await service.tick()
            state = await store.state(BOT)
            if int(state["paused"] or 0):
                break
            clock.now = max(clock.now + 60_000, int(state["next_wake_at"] or clock.now))
        rows = await db.fetchall(
            "SELECT s.id, s.status, s.rounds, s.calls, s.transcript, s.backfill, s.summary, "
            "s.error, e.fragment, e.salience FROM story_scenes s "
            "LEFT JOIN events e ON e.id=s.event_id WHERE s.bot_id=? ORDER BY s.id",
            (BOT,),
        )
        canon = await db.fetchall(
            "SELECT name, body, scene_id FROM story_canon WHERE bot_id=? AND origin='play'", (BOT,)
        )
        return {
            "arm": arm.name,
            "index": index,
            "rejected": audit.rejected,
            "guard_person": audit.guard_person,
            "guard_speech": audit.guard_speech,
            "gm": {
                "model": providers["gm"].model,
                "thinking": arm.thinking,
                "requests": providers["gm"].requests,
                "tokens_in": providers["gm"].prompt_tokens,
                "tokens_out": providers["gm"].completion_tokens,
                "reasoning": providers["gm"].reasoning_tokens,
                "seconds": round(providers["gm"].seconds, 1),
            },
            "actor_tokens_out": providers["actor"].completion_tokens,
            "events": audit.scenes,
            "past": audit.past,
            "canon": canon,
            "scenes": [
                {**row, "transcript": json.loads(row["transcript"] or "[]")} for row in rows
            ],
        }
    finally:
        await db.close()


async def judge_scene(scene: dict, base: str, key: str, model: str) -> dict | None:
    lines: list[str] = []
    n = a = 0
    for turn in scene["transcript"]:
        if turn.get("role") == "narrator":
            n += 1
            lines.append(f"N{n}: {turn.get('text')}")
        else:
            a += 1
            lines.append(f"A{a}: {turn.get('text')}")
    async with SEM:
        result = await UrllibClient().post_json(
            chat_url(base),
            {
                "model": model,
                "temperature": 0,
                "response_format": {"type": "json_object"},
                "messages": [
                    {"role": "system", "content": JUDGE_SYSTEM},
                    {"role": "user", "content": "\n".join(lines)},
                ],
            },
            {"Authorization": f"Bearer {key}"},
            timeout_s=120.0,
        )
    if not result.ok or not isinstance(result.body, dict):
        return None
    content = ((result.body.get("choices") or [{}])[0].get("message") or {}).get("content")
    verdict = extract_json(content)
    return verdict if isinstance(verdict, dict) else None


def strip_quotes(text: str) -> str:
    return QUOTED.sub("", text or "")


def style_slip(variant: str, text: str) -> bool:
    """叙述有没有按要求的人称写（引号里的对话不算）。"""
    body = strip_quotes(text)
    if variant == "second":
        return "我" in body
    if variant == "first":
        return "你" in body
    return "你" in body or "我" in body


def acts_context(scene: dict, item_id: str) -> dict[str, str]:
    """被判“替她行动”的那条叙述，连同她上一步写了什么，方便人工复核。"""
    n = 0
    previous = ""
    for turn in scene["transcript"]:
        if turn.get("role") == "narrator":
            n += 1
            if f"N{n}" == item_id:
                return {"her_last": previous, "narration": str(turn.get("text") or "")}
        else:
            previous = str(turn.get("text") or "")
    return {"her_last": previous, "narration": ""}


def report(arms: list[Arm], good: list[dict[str, Any]]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for arm in arms:
        mine = [stream for stream in good if stream["arm"] == arm.name]
        scenes = [scene for stream in mine for scene in stream["scenes"]]
        ok = [scene for scene in scenes if scene["status"] == "ok"]
        judged = [scene for scene in ok if scene.get("verdict")]
        n_total = sum(1 for s in ok for t in s["transcript"] if t.get("role") == "narrator")
        slips = sum(
            1 for s in ok for t in s["transcript"]
            if t.get("role") == "narrator" and style_slip(arm.narration, t.get("text") or "")
        )
        interior: list[str] = []
        acts: list[dict[str, Any]] = []
        outcome: list[str] = []
        for stream in mine:
            for scene in stream["scenes"]:
                verdict = scene.get("verdict") or {}
                for item in verdict.get("N") or []:
                    if item.get("interior"):
                        interior.append(str(item.get("interior_quote") or "?"))
                    if item.get("acts"):
                        acts.append(
                            {
                                "where": f"{stream['index']}/#{scene['id']}/{item.get('id')}",
                                "quote": str(item.get("acts_quote") or "?"),
                                **acts_context(scene, str(item.get("id"))),
                            }
                        )
                for item in verdict.get("A") or []:
                    if item.get("outcome"):
                        outcome.append(str(item.get("quote") or "?"))
        wakes = [e["wake_minutes"] for s in mine for e in s["events"] if "wake_minutes" in e]
        canon = [row for s in mine for row in s["canon"]]
        salience = [int(s["salience"]) for s in ok if s.get("salience") is not None]
        gm = [s["gm"] for s in mine]
        picked = [len(p.get("scores") or {}) for s in mine for p in s["past"]]
        summary[arm.name] = {
            "arm": asdict(arm),
            "ok": len(ok),
            "scenes": len(scenes),
            "rounds_avg": round(sum(s["rounds"] for s in ok) / max(1, len(ok)), 2),
            "calls_avg": round(sum(s["calls"] for s in ok) / max(1, len(ok)), 1),
            "rejected": sum(s["rejected"] for s in mine),
            "guard_person": sum(s["guard_person"] for s in mine),
            "guard_speech": sum(s["guard_speech"] for s in mine),
            "narrations": n_total,
            "judged_scenes": len(judged),
            "interior": interior,
            "acts": acts,
            "outcome": outcome,
            "style_slips": slips,
            "wake_minutes_median": statistics.median(wakes) if wakes else None,
            "wake_minutes_mean": round(statistics.mean(wakes), 1) if wakes else None,
            "canon_added": len(canon),
            "canon_names": [row["name"] for row in canon],
            "salience": {level: salience.count(level) for level in range(1, 6)},
            "gm_requests": sum(g["requests"] for g in gm),
            "gm_tokens_in": sum(g["tokens_in"] for g in gm),
            "gm_tokens_out": sum(g["tokens_out"] for g in gm),
            "gm_reasoning": sum(g["reasoning"] for g in gm),
            "gm_seconds": round(sum(g["seconds"] for g in gm), 1),
            "past_judged": len(picked),
        }
    return summary


def print_report(summary: dict[str, Any]) -> None:
    for name, row in summary.items():
        print(f"===== {name} =====")
        print(
            f"场景：演成 {row['ok']} / {row['scenes']}；平均 {row['rounds_avg']} 轮，"
            f"{row['calls_avg']} 次调用；输出被打回 {row['rejected']} 次"
            f"（真人保护 {row['guard_person']}，替她说话 {row['guard_speech']}）"
        )
        print(
            f"叙述 {row['narrations']} 条（已审 {row['judged_scenes']} 场）：写了她的内心 "
            f"{len(row['interior'])}，替她行动 {len(row['acts'])}，没按人称写 {row['style_slips']}；"
            f"她自己写了结果 {len(row['outcome'])}"
        )
        print(
            f"预约间隔：中位 {row['wake_minutes_median']} 分钟，平均 {row['wake_minutes_mean']}；"
            f"新设定 {row['canon_added']} 条；显著度分布 {row['salience']}"
        )
        gm_requests = max(1, row["gm_requests"])
        print(
            f"背景模型：{row['gm_requests']} 次请求，平均输入 {row['gm_tokens_in'] // gm_requests}、"
            f"输出 {row['gm_tokens_out'] // gm_requests}（思考 {row['gm_reasoning'] // gm_requests}）"
            f" token，平均 {row['gm_seconds'] / gm_requests:.1f} 秒／次"
        )
        for item in row["acts"]:
            print(f"  [替她行动 {item['where']}] {item['quote']}")
        for quote in row["interior"]:
            print(f"  [内心] {quote}")
        for quote in row["outcome"]:
            print(f"  [她写结果] {quote}")
        print()


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tag", default="run")
    parser.add_argument("--arm", action="append", default=[])
    parser.add_argument("--scenes", type=int, default=5)
    parser.add_argument("--streams", type=int, default=len(STARTS))
    parser.add_argument("--no-judge", action="store_true")
    args = parser.parse_args()
    base = os.environ.get("DSH_TEST_TEXT_BASE_URL", "").strip()
    key = os.environ.get("DSH_TEST_TEXT_API_KEY", "").strip()
    model = os.environ.get("DSH_TEST_TEXT_MODEL", "deepseek-chat").strip() or "deepseek-chat"
    judge_model = os.environ.get("DSH_TEST_JUDGE_MODEL", "deepseek-chat").strip() or "deepseek-chat"
    if not base or not key:
        print("需要 DSH_TEST_TEXT_BASE_URL / DSH_TEST_TEXT_API_KEY")
        return 2
    real_jev = bool(
        os.environ.get("DSH_TEST_JEV_BASE_URL", "").strip()
        and os.environ.get("DSH_TEST_JEV_API_KEY", "").strip()
    )
    arms = [Arm.parse(spec) for spec in args.arm] or [Arm("second"), Arm("first", "first")]
    logging.basicConfig(level=logging.ERROR)
    out = OUT / args.tag
    out.mkdir(parents=True, exist_ok=True)

    streams = await asyncio.gather(
        *[
            run_stream(arm, index, args.scenes, base, key, model, real_jev)
            for arm in arms
            for index in range(args.streams)
        ],
        return_exceptions=True,
    )
    good = [item for item in streams if isinstance(item, dict)]
    for item in streams:
        if not isinstance(item, dict):
            print("时间线失败：", repr(item)[:200])

    if not args.no_judge:
        jobs = [
            scene for stream in good for scene in stream["scenes"] if scene["status"] == "ok"
        ]
        verdicts = await asyncio.gather(
            *[judge_scene(scene, base, key, judge_model) for scene in jobs]
        )
        for scene, verdict in zip(jobs, verdicts):
            scene["verdict"] = verdict

    summary = report(arms, good)
    (out / "results.json").write_text(
        json.dumps(good, ensure_ascii=False, indent=1, default=str), encoding="utf-8"
    )
    (out / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=1, default=str), encoding="utf-8"
    )
    print(
        f"对话模型 {model}；判断模型 {'真实' if real_jev else '不可用'}；审稿 {judge_model}；"
        f"每组 {args.streams} 条时间线 × {args.scenes} 场\n"
    )
    print_report(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
