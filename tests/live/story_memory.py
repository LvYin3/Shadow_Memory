"""背景模型长期记忆的实测（手动运行，不进 CI）。

先用 ``ab_story.py --streams 1 --scenes 30 --no-judge`` 演出一段历史，再手写探针：
每个探针给出一个此刻的处境（状态卡 + 上一场的结尾），以及哪几场旧场景和它相关。

    # 检索质量：字面前三 / 判断模型挑（候选 10 条）/ 判断模型挑（全部候选）
    .venv\\Scripts\\python.exe -X utf8 astrbot_plugin_shadow_memory/tests/live/story_memory.py `
        probe --db .pytest_tmp/ab/_work/hist_0/memory.db --probes probes.json
    # 端到端：同一个处境各演一场，开 / 关旧场景回想，审稿模型看新的一场有没有接上旧事
    ... story_memory.py e2e --db ... --probes probes.json --repeat 1

探针文件：``[{"id": "p1", "where": "", "doing": "", "ending": "", "expect": [12], "fact": ""}]``。
``ending`` 是假设的上一场结尾（她最后看到的），``fact`` 是旧场景里应当被接上的那件事。
密钥只从环境变量读（与 ab_story.py 相同），结果写在 ``.pytest_tmp/story_memory/``。
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
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from astrbot_plugin_shadow_memory.core.config import ConfigView  # noqa: E402
from astrbot_plugin_shadow_memory.core.db import Database  # noqa: E402
from astrbot_plugin_shadow_memory.core.decision import JevClient, chat_url  # noqa: E402
from astrbot_plugin_shadow_memory.core.jsonx import extract_json  # noqa: E402
from astrbot_plugin_shadow_memory.core.textgen import TextClient  # noqa: E402
from astrbot_plugin_shadow_memory.story import oracle  # noqa: E402
from astrbot_plugin_shadow_memory.story.card import StatusCard, render_for_gm  # noqa: E402
from astrbot_plugin_shadow_memory.story.director import Director  # noqa: E402
from astrbot_plugin_shadow_memory.story.llm import StoryLLM  # noqa: E402
from astrbot_plugin_shadow_memory.story.recall import PastScenes, _literal, scene_line  # noqa: E402
from astrbot_plugin_shadow_memory.story.store import StoryStore  # noqa: E402
from astrbot_plugin_shadow_memory.tests.live.ab_story import CountingAudit, Limited  # noqa: E402
from astrbot_plugin_shadow_memory.tests.live.smoke_models import UrllibClient  # noqa: E402
from astrbot_plugin_shadow_memory.tests.live.smoke_story import (  # noqa: E402
    BASE_CARD,
    BOT,
    Clock,
    decision_settings,
)
from astrbot_plugin_shadow_memory.tests.unit.helpers import build_stack  # noqa: E402

OUT = ROOT / ".pytest_tmp" / "story_memory"
RECENT = 3

E2E_JUDGE = """下面是一个故事里新演的一场，以及这个故事更早时发生过的一件事。
判断新的一场里，主持人写的内容和那件旧事的关系：
- uses：有没有自然地接上或提到那件旧事（人物、承诺、物品、没收尾的事），true / false；
- contradicts：有没有和那件旧事矛盾（例如把已经确定的事写成另一个样子），true / false；
- quote：最能说明问题的原文片段（不超过 40 字），都没有就给空字符串。
只输出 JSON：{"uses": false, "contradicts": false, "quote": ""}"""


def env_keys() -> tuple[str, str, str]:
    base = os.environ.get("DSH_TEST_TEXT_BASE_URL", "").strip()
    key = os.environ.get("DSH_TEST_TEXT_API_KEY", "").strip()
    model = os.environ.get("DSH_TEST_TEXT_MODEL", "deepseek-chat").strip() or "deepseek-chat"
    if not base or not key:
        raise SystemExit("需要 DSH_TEST_TEXT_BASE_URL / DSH_TEST_TEXT_API_KEY")
    return base, key, model


def configs_for(base: str, key: str, model: str, **story: Any):
    raw = {
        "story": {"enabled": True, "base_card": BASE_CARD, **story},
        "text": {"base_url": base, "api_key": key, "model": model, "max_tokens": 1200},
        "decision": decision_settings(base, key, model),
    }

    def configs(bot_id: str = "") -> ConfigView:
        return ConfigView(raw, bot_id)

    return configs


async def history(db: Database) -> list[dict[str, Any]]:
    return await db.fetchall(
        "SELECT id, started_at, ended_at, summary FROM story_scenes WHERE bot_id=? "
        "AND status='ok' ORDER BY id",
        (BOT,),
    )


def probe_state(probe: dict[str, Any], rows: list[dict[str, Any]], now: int) -> tuple[str, dict]:
    recent = [scene_line(row) for row in rows[-RECENT:]]
    card = StatusCard(
        where=probe.get("where", ""), doing=probe.get("doing", ""), until=now + 3_600_000,
        updated_at=now,
    )
    state = {
        "now": oracle.real_moment(now)["text"],
        "her_status": render_for_gm(card, now),
        "recent_scenes": recent,
        "last_ending": str(probe.get("ending") or "")[:400],
    }
    return f"{card.where} {card.doing} {probe.get('ending', '')}", state


# --------------------------------------------------------------------- 检索质量
async def run_probe(args: argparse.Namespace) -> int:
    base, key, model = env_keys()
    probes = json.loads(Path(args.probes).read_text(encoding="utf-8"))
    db = Database(Path(args.db))
    await db.connect()
    try:
        rows = await history(db)
        now = int(rows[-1]["ended_at"]) + 600_000
        configs = configs_for(base, key, model)
        audit = CountingAudit()
        jev = JevClient(configs(""), UrllibClient(), audit)
        past = PastScenes(StoryStore(db), jev, audit)
        older = rows[:-RECENT]
        results = []
        for probe in probes:
            query, state = probe_state(probe, rows, now)
            expect = set(probe["expect"])
            literal = [row["id"] for _, row in sorted(_literal(older, query), key=lambda x: -x[0])]
            entry: dict[str, Any] = {
                "id": probe["id"],
                "expect": sorted(expect),
                "literal_top3": literal[:3],
                "literal_hit": bool(expect & set(literal[:3])),
            }
            for label, candidates in (("jev10", 10), ("jev_all", 30)):
                runs = []
                for _ in range(args.repeat):
                    started = time.perf_counter()
                    pick = await past.pick(
                        BOT, skip=RECENT, query=query, state=state, limit=3,
                        candidates=candidates, threshold=args.threshold,
                    )
                    elapsed = time.perf_counter() - started
                    ids = [int(line.split()[0][1:]) for line in pick.lines]
                    top = sorted(pick.scores.items(), key=lambda kv: -kv[1])[:5]
                    runs.append(
                        {
                            "picked": ids,
                            "hit": bool(expect & set(ids)),
                            "candidate_has_expect": bool(expect & set(pick.scores)),
                            "expect_scores": {i: round(pick.scores.get(i, -1), 2) for i in expect},
                            "top5": [(i, round(s, 2)) for i, s in top],
                            "reason": pick.reason,
                            "seconds": round(elapsed, 2),
                        }
                    )
                entry[label] = runs
            results.append(entry)
            print(
                f"{probe['id']} 期望 {sorted(expect)} | 字面 {entry['literal_top3']}"
                f"{' ✓' if entry['literal_hit'] else ''} | "
                + " | ".join(
                    f"{label} {run['picked']}{' ✓' if run['hit'] else ''}"
                    f"（期望分 {run['expect_scores']}，候选含期望 {run['candidate_has_expect']}）"
                    for label in ("jev10", "jev_all")
                    for run in entry[label]
                )
            )
        total = len(results)
        print(
            f"\n命中（期望场景出现在带上的前 3 条里）：字面 "
            f"{sum(r['literal_hit'] for r in results)}/{total}；"
            + "；".join(
                f"{label} {sum(run['hit'] for r in results for run in r[label])}/"
                f"{total * args.repeat}"
                for label in ("jev10", "jev_all")
            )
        )
        control = [r for r in results if not r["expect"]]
        for label in ("jev10", "jev_all"):
            seconds = [run["seconds"] for r in results for run in r[label]]
            noise = [len(run["picked"]) for r in control for run in r[label]]
            print(
                f"{label}：平均 {sum(seconds) / max(1, len(seconds)):.2f} 秒／次，"
                f"无关对照平均带上 {sum(noise) / max(1, len(noise)):.1f} 条"
            )
        print(f"判断模型统计：{jev.stats}")
        OUT.mkdir(parents=True, exist_ok=True)
        (OUT / "probe.json").write_text(
            json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8"
        )
    finally:
        await db.close()
    return 0


# --------------------------------------------------------------------- 端到端
async def play_once(
    source: Path, probe: dict[str, Any], past: int, tag: str, base: str, key: str, model: str
) -> dict[str, Any]:
    workdir = OUT / "_work" / tag
    shutil.rmtree(workdir, ignore_errors=True)
    workdir.mkdir(parents=True, exist_ok=True)
    target = workdir / "memory.db"
    shutil.copy(source, target)
    configs = configs_for(base, key, model, past_scenes=past)
    audit = CountingAudit()
    db = Database(target)
    await db.connect()
    try:
        rows = await history(db)
        clock = Clock()
        clock.now = int(rows[-1]["ended_at"]) + 20 * 60_000
        store = StoryStore(db, now_fn=clock)
        # 把探针的处境写成“上一场”：结尾是她最后看到的，状态卡是她此刻在做的
        await store.insert_scene(
            BOT, cause="manual", status="ok", started_at=clock.now - 15 * 60_000,
            ended_at=clock.now - 10 * 60_000,
            transcript=[{"role": "narrator", "text": probe["ending"]}],
            summary=probe.get("summary") or probe["ending"][:100],
        )
        card = StatusCard(
            where=probe["where"], doing=probe["doing"], until=clock.now + 1_800_000,
            updated_at=clock.now - 10 * 60_000,
        )
        await store.update_state(
            BOT, card=card.to_json(), last_scene_at=clock.now - 10 * 60_000, paused=0, failures=0
        )
        stack = build_stack(
            db, configs, text=TextClient(configs(""), UrllibClient(), audit),
            jev=JevClient(configs(""), UrllibClient(), audit),
        )
        providers = {"gm": Limited(base, key, model, "gm"), "actor": Limited(base, key, model, "actor")}

        async def resolve(role: str, bot_id: str):
            return providers[role]

        async def base_card(bot_id: str):
            return BASE_CARD, "memory"

        director = Director(
            db=db, configs=configs, store=store, memory=stack.service,
            llm=StoryLLM(resolve, audit), audit=audit, base_card=base_card,
            rng=random.Random(7), now_fn=clock,
        )
        result = await director.run(BOT, manual=True)
        scene = await db.fetchone(
            "SELECT transcript, summary FROM story_scenes WHERE id=?", (result.scene_id,)
        )
        return {
            "transcript": json.loads(scene["transcript"] or "[]"),
            "summary": scene["summary"],
            "past": audit.past,
        }
    finally:
        await db.close()


async def judge(scene: dict[str, Any], fact: str, base: str, key: str) -> dict[str, Any] | None:
    text = "\n".join(
        f"{'主持人' if turn.get('role') == 'narrator' else '她'}：{turn.get('text')}"
        for turn in scene["transcript"]
    )
    result = await UrllibClient().post_json(
        chat_url(base),
        {
            "model": "deepseek-chat",
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": E2E_JUDGE},
                {"role": "user", "content": f"# 更早发生过的事\n{fact}\n\n# 新的一场\n{text}"},
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


async def run_e2e(args: argparse.Namespace) -> int:
    base, key, model = env_keys()
    probes = json.loads(Path(args.probes).read_text(encoding="utf-8"))
    source = Path(args.db)
    jobs = [
        (probe, past, f"{probe['id']}_{'on' if past else 'off'}_{rep}")
        for probe in probes
        for past in (3, 0)
        for rep in range(args.repeat)
    ]
    scenes = await asyncio.gather(
        *[play_once(source, probe, past, tag, base, key, model) for probe, past, tag in jobs],
        return_exceptions=True,
    )
    results = []
    for (probe, past, tag), scene in zip(jobs, scenes):
        if not isinstance(scene, dict):
            print(f"{tag} 没演成：{scene!r}"[:200])
            continue
        verdict = await judge(scene, probe["fact"], base, key)
        results.append(
            {"tag": tag, "probe": probe["id"], **scene, "past_scenes": past, "verdict": verdict}
        )
        brought = [p.get("picked") for p in scene["past"]]
        print(f"{tag}: {verdict}  带上旧场景 {brought}")
    for past in (3, 0):
        mine = [r for r in results if r["past_scenes"] == past and r["verdict"]]
        uses = sum(bool(r["verdict"].get("uses")) for r in mine)
        bad = sum(bool(r["verdict"].get("contradicts")) for r in mine)
        print(f"past_scenes={past}：接上旧事 {uses}/{len(mine)}，矛盾 {bad}/{len(mine)}")
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "e2e.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=1, default=str), encoding="utf-8"
    )
    return 0


async def run_extend(args: argparse.Namespace) -> int:
    """在已有的历史上用手动唤醒（不扣额度）接着演，按背景模型的预约拨时钟。"""
    base, key, model = env_keys()
    configs = configs_for(base, key, model)
    audit = CountingAudit()
    db = Database(Path(args.db))
    await db.connect()
    try:
        rows = await history(db)
        clock = Clock()
        store = StoryStore(db, now_fn=clock)
        state = await store.state(BOT)
        clock.now = max(int(rows[-1]["ended_at"]) + 60_000, int(state["next_wake_at"] or 0))
        stack = build_stack(
            db, configs, text=TextClient(configs(""), UrllibClient(), audit),
            jev=JevClient(configs(""), UrllibClient(), audit),
        )
        providers = {"gm": Limited(base, key, model, "gm"), "actor": Limited(base, key, model, "actor")}

        async def resolve(role: str, bot_id: str):
            return providers[role]

        async def base_card(bot_id: str):
            return BASE_CARD, "memory"

        director = Director(
            db=db, configs=configs, store=store, memory=stack.service,
            llm=StoryLLM(resolve, audit), audit=audit, base_card=base_card,
            rng=random.Random(len(rows)), now_fn=clock,
        )
        for _ in range(args.scenes):
            result = await director.run(BOT, manual=True)
            print(f"#{result.scene_id} 下次 {round((result.next_wake_at - clock.now) / 60_000)} 分钟后")
            clock.now = max(clock.now + 60_000, result.next_wake_at)
    finally:
        await db.close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("probe", "e2e"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--db", required=True)
        cmd.add_argument("--probes", required=True)
        cmd.add_argument("--repeat", type=int, default=1)
        cmd.add_argument("--threshold", type=float, default=0.6)
    extend = sub.add_parser("extend")
    extend.add_argument("--db", required=True)
    extend.add_argument("--scenes", type=int, default=9)
    args = parser.parse_args()
    logging.basicConfig(level=logging.ERROR)
    runner = {"probe": run_probe, "e2e": run_e2e, "extend": run_extend}[args.cmd]
    return asyncio.run(runner(args))


if __name__ == "__main__":
    raise SystemExit(main())
