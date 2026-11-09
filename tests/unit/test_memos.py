from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from astrbot_plugin_shadow_memory.core.audit import Audit
from astrbot_plugin_shadow_memory.interaction.memos import MemoService

NOW = 1_800_000_000_000


class FakeScheduler:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.counter = 0

    async def __call__(self, **kwargs) -> str:
        self.counter += 1
        self.calls.append(kwargs)
        return f"job-{self.counter}"


class FakeDeleter:
    def __init__(self) -> None:
        self.deleted: list[str] = []

    async def __call__(self, job_id: str) -> None:
        self.deleted.append(job_id)


@pytest.fixture
def memos(db):
    scheduler = FakeScheduler()
    deleter = FakeDeleter()
    service = MemoService(db, Audit(), scheduler, deleter, now_fn=lambda: NOW)
    service.scheduler = scheduler  # type: ignore[attr-defined]
    service.deleter = deleter  # type: ignore[attr-defined]
    return service


def _iso(offset_seconds: int) -> str:
    return (
        datetime.fromtimestamp((NOW + offset_seconds * 1000) / 1000.0, tz=timezone.utc)
        .isoformat()
    )


async def test_create_one_shot(memos):
    result = await memos.create(
        "bot1", note="提醒我给小明回消息", umo="umo1", sender_id="u1", run_at=_iso(3600)
    )
    assert result["memo_id"] == 1
    assert result["job_id"] == "job-1"
    call = memos.scheduler.calls[0]
    assert call["run_once"] is True
    assert call["cron_expression"] is None
    assert call["payload"]["session"] == "umo1"
    assert "memo_id=1" in call["payload"]["note"]


async def test_create_requires_exactly_one_trigger(memos):
    with pytest.raises(ValueError):
        await memos.create("bot1", note="x", umo="umo1")
    with pytest.raises(ValueError):
        await memos.create("bot1", note="x", umo="umo1", run_at=_iso(10), cron="30 3 * * *")


async def test_create_rejects_past_time(memos):
    with pytest.raises(ValueError):
        await memos.create("bot1", note="x", umo="umo1", run_at=_iso(-3600))


async def test_create_rejects_bad_cron(memos):
    with pytest.raises(ValueError):
        await memos.create("bot1", note="x", umo="umo1", cron="0 30 3 * * *")


async def test_create_recurring(memos):
    result = await memos.create("bot1", note="每天提醒", umo="umo1", cron="30 3 * * *")
    assert result["cron"] == "30 3 * * *"
    call = memos.scheduler.calls[0]
    assert call["run_once"] is False


async def test_get_marks_fired(memos):
    await memos.create("bot1", note="提醒", umo="umo1", run_at=_iso(3600))
    row = await memos.get("bot1", 1)
    assert row is not None
    assert row["fired_at"] is not None
    assert row["status"] == "fired"


async def test_get_missing_returns_none(memos):
    assert await memos.get("bot1", 99) is None


async def test_list_and_render(memos):
    await memos.create("bot1", note="提醒A", umo="umo1", run_at=_iso(3600))
    rows = await memos.list("bot1")
    assert len(rows) == 1
    assert "#1" in memos.render(rows[0])


async def test_edit_reschedules(memos):
    await memos.create("bot1", note="提醒", umo="umo1", run_at=_iso(3600))
    await memos.edit("bot1", 1, note="改过的提醒")
    assert memos.deleter.deleted == ["job-1"]
    assert len(memos.scheduler.calls) == 2


async def test_delete_removes_job(memos):
    await memos.create("bot1", note="提醒", umo="umo1", run_at=_iso(3600))
    assert await memos.delete("bot1", 1) is True
    assert memos.deleter.deleted == ["job-1"]
    assert await memos.list("bot1") == []


async def test_reconcile_marks_lost(memos):
    await memos.create("bot1", note="提醒", umo="umo1", run_at=_iso(3600))
    lost = await memos.reconcile("bot1", {"other-job"})
    assert lost == 1
    rows = await memos.list("bot1")
    assert rows[0]["status"] == "lost"


async def test_edit_missing_raises(memos):
    with pytest.raises(ValueError):
        await memos.edit("bot1", 42, note="x")


def test_parse_time_tolerates_naive():
    parsed = MemoService._parse_time("2026-11-03T08:00:00")
    assert parsed.tzinfo is not None
    assert parsed == parsed.astimezone()
    assert datetime.now(timezone.utc) - datetime.fromtimestamp(
        parsed.timestamp(), tz=timezone.utc
    ) < timedelta(days=365 * 20)
