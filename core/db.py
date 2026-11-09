"""SQLite 存储层。

单进程单连接 + WAL + 线程池执行；所有读写都经过同一把 asyncio 锁，
事务内部的调用复用同一连接（不再抢锁）。
"""

from __future__ import annotations

import asyncio
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Iterable, Sequence

SCHEMA_VERSION = 2

DDL = """
CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);

CREATE TABLE IF NOT EXISTS sessions (
  bot_id TEXT NOT NULL,
  umo TEXT NOT NULL,
  name TEXT,
  type TEXT,
  last_message_at INTEGER,
  last_summary TEXT,
  PRIMARY KEY (bot_id, umo)
);

CREATE TABLE IF NOT EXISTS chat_sessions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  bot_id TEXT NOT NULL,
  umo TEXT NOT NULL,
  started_at INTEGER NOT NULL,
  ended_at INTEGER,
  participants TEXT
);

CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  bot_id TEXT NOT NULL,
  umo TEXT NOT NULL,
  chat_session_id INTEGER,
  kind TEXT NOT NULL DEFAULT 'episodic',
  started_at INTEGER NOT NULL,
  ended_at INTEGER,
  participants TEXT,
  turn_count INTEGER NOT NULL DEFAULT 0,
  char_count INTEGER NOT NULL DEFAULT 0,
  fragment TEXT,
  summary TEXT,
  excerpts TEXT,
  entities TEXT,
  source_event_ids TEXT,
  storage_strength REAL NOT NULL DEFAULT 7.0,
  retrievability REAL NOT NULL DEFAULT 1.0,
  last_recalled_at INTEGER,
  last_injected_at INTEGER,
  salience INTEGER,
  conflict INTEGER NOT NULL DEFAULT 0,
  truncated INTEGER NOT NULL DEFAULT 0,
  merged_into INTEGER,
  status TEXT NOT NULL DEFAULT 'open',
  created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS event_turns (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
  seq INTEGER NOT NULL,
  role TEXT NOT NULL,
  speaker_id TEXT,
  speaker_name TEXT,
  text TEXT NOT NULL,
  ts INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS entities (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  bot_id TEXT NOT NULL,
  name TEXT NOT NULL,
  canonical TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  UNIQUE (bot_id, canonical)
);

CREATE TABLE IF NOT EXISTS event_entities (
  event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
  entity_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
  PRIMARY KEY (event_id, entity_id)
);

CREATE TABLE IF NOT EXISTS event_edges (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  bot_id TEXT NOT NULL,
  src_event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
  dst_event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
  weight INTEGER NOT NULL DEFAULT 1,
  last_seen_at INTEGER NOT NULL,
  UNIQUE (bot_id, src_event_id, dst_event_id)
);

CREATE TABLE IF NOT EXISTS persona_entries (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  bot_id TEXT NOT NULL,
  layer TEXT NOT NULL CHECK (layer IN ('core', 'edge', 'tension')),
  text TEXT NOT NULL,
  source_event_ids TEXT NOT NULL,
  created_day INTEGER NOT NULL,
  last_edited_day INTEGER NOT NULL,
  active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS persona_versions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  bot_id TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  reason TEXT NOT NULL,
  snapshot TEXT NOT NULL,
  applied_ops TEXT,
  digest TEXT
);

CREATE TABLE IF NOT EXISTS facts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  bot_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  subject_id TEXT,
  text TEXT NOT NULL,
  key_fact INTEGER NOT NULL DEFAULT 0,
  storage_strength REAL NOT NULL DEFAULT 30.0,
  retrievability REAL NOT NULL DEFAULT 1.0,
  last_recalled_at INTEGER,
  valid_from INTEGER,
  valid_to INTEGER,
  superseded_by INTEGER,
  archived INTEGER NOT NULL DEFAULT 0,
  source_event_id INTEGER,
  created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS fact_candidates (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  bot_id TEXT NOT NULL,
  umo TEXT NOT NULL,
  text TEXT NOT NULL,
  salience INTEGER NOT NULL DEFAULT 2,
  created_at INTEGER NOT NULL,
  consumed INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS notes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  bot_id TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('note', 'diary')),
  title TEXT,
  body TEXT NOT NULL,
  tags TEXT,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS memos (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  bot_id TEXT NOT NULL,
  note TEXT NOT NULL,
  run_at INTEGER,
  cron TEXT,
  target_session TEXT,
  status TEXT NOT NULL DEFAULT 'scheduled',
  job_id TEXT,
  fired_at INTEGER,
  skip_reason TEXT,
  created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS insights (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  bot_id TEXT NOT NULL,
  text TEXT NOT NULL,
  source_event_ids TEXT NOT NULL,
  used_in_version INTEGER,
  created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS consolidation_logs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  bot_id TEXT NOT NULL,
  cluster_size INTEGER,
  gist_id INTEGER,
  superseded TEXT,
  insights INTEGER,
  status TEXT NOT NULL,
  created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS gate_logs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  bot_id TEXT NOT NULL,
  umo TEXT NOT NULL,
  message_id TEXT,
  p_reply REAL,
  threshold REAL,
  decision TEXT NOT NULL,
  reason TEXT,
  latency_ms INTEGER,
  created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS inject_logs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  bot_id TEXT NOT NULL,
  umo TEXT NOT NULL,
  event_ids TEXT,
  persona_version INTEGER,
  cache_hit INTEGER,
  cache_other INTEGER,
  latency_ms INTEGER,
  reason TEXT,
  p_top REAL,
  p_none REAL,
  created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS distill_logs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  bot_id TEXT NOT NULL,
  cycle_at INTEGER NOT NULL,
  gist_count INTEGER,
  insight_count INTEGER,
  proposed INTEGER,
  applied INTEGER,
  rejected INTEGER,
  reason TEXT,
  created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS qzone_logs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  bot_id TEXT NOT NULL,
  platform TEXT,
  action TEXT,
  content_len INTEGER,
  images INTEGER,
  ugc_right INTEGER,
  tid TEXT,
  status TEXT NOT NULL,
  error TEXT,
  created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS media_items (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  bot_id TEXT NOT NULL,
  filename TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  tags TEXT,
  source TEXT,
  source_ref TEXT,
  sender_id TEXT,
  sender_name TEXT,
  source_umo TEXT,
  size INTEGER NOT NULL,
  created_at INTEGER NOT NULL,
  last_used_at INTEGER,
  use_count INTEGER NOT NULL DEFAULT 0,
  UNIQUE (bot_id, filename)
);

CREATE TABLE IF NOT EXISTS media_logs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  bot_id TEXT NOT NULL,
  action TEXT NOT NULL,
  filename TEXT,
  size INTEGER,
  source TEXT,
  status TEXT NOT NULL,
  error TEXT,
  created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS kv (
  bot_id TEXT NOT NULL,
  key TEXT NOT NULL,
  value TEXT,
  updated_at INTEGER NOT NULL,
  PRIMARY KEY (bot_id, key)
);

CREATE TABLE IF NOT EXISTS session_messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  bot_id TEXT NOT NULL,
  umo TEXT NOT NULL,
  role TEXT NOT NULL,
  speaker TEXT,
  text TEXT NOT NULL,
  ts INTEGER NOT NULL
);
"""

DDL_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_event_turns_event ON event_turns(event_id, seq);
CREATE INDEX IF NOT EXISTS idx_events_status ON events(bot_id, status, ended_at);
CREATE INDEX IF NOT EXISTS idx_events_r ON events(bot_id, retrievability);
CREATE INDEX IF NOT EXISTS idx_events_merged ON events(bot_id, merged_into);
CREATE INDEX IF NOT EXISTS idx_events_kind ON events(bot_id, kind, status);
CREATE INDEX IF NOT EXISTS idx_edges_src ON event_edges(bot_id, src_event_id);
CREATE INDEX IF NOT EXISTS idx_edges_dst ON event_edges(bot_id, dst_event_id);
CREATE INDEX IF NOT EXISTS idx_event_entities_event ON event_entities(event_id);
CREATE INDEX IF NOT EXISTS idx_event_entities_entity ON event_entities(entity_id);
CREATE INDEX IF NOT EXISTS idx_entities_bot ON entities(bot_id, canonical);
CREATE INDEX IF NOT EXISTS idx_persona_bot ON persona_entries(bot_id, layer, active);
CREATE INDEX IF NOT EXISTS idx_persona_versions_bot ON persona_versions(bot_id, created_at);
CREATE INDEX IF NOT EXISTS idx_facts_bot ON facts(bot_id, archived, subject_id);
CREATE INDEX IF NOT EXISTS idx_facts_valid ON facts(bot_id, valid_to);
CREATE INDEX IF NOT EXISTS idx_fact_candidates_bot ON fact_candidates(bot_id, consumed);
CREATE INDEX IF NOT EXISTS idx_insights_bot ON insights(bot_id, used_in_version);
CREATE INDEX IF NOT EXISTS idx_notes_bot ON notes(bot_id, kind, updated_at);
CREATE INDEX IF NOT EXISTS idx_memos_bot ON memos(bot_id, status, run_at);
CREATE INDEX IF NOT EXISTS idx_gate_logs_bot_time ON gate_logs(bot_id, created_at);
CREATE INDEX IF NOT EXISTS idx_inject_logs_bot_time ON inject_logs(bot_id, created_at);
CREATE INDEX IF NOT EXISTS idx_distill_logs_bot_time ON distill_logs(bot_id, created_at);
CREATE INDEX IF NOT EXISTS idx_consolidation_bot_time ON consolidation_logs(bot_id, created_at);
CREATE INDEX IF NOT EXISTS idx_qzone_logs_bot_time ON qzone_logs(bot_id, created_at);
CREATE INDEX IF NOT EXISTS idx_media_items_bot_time ON media_items(bot_id, created_at);
CREATE INDEX IF NOT EXISTS idx_media_logs_bot_time ON media_logs(bot_id, created_at);
CREATE INDEX IF NOT EXISTS idx_session_messages ON session_messages(bot_id, umo, id);
"""


class Database:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._conn: sqlite3.Connection | None = None
        self._lock = asyncio.Lock()
        self._in_tx = False

    # ------------------------------------------------------------------ 连接
    async def connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(
            str(self.path),
            timeout=5.0,
            check_same_thread=False,
            isolation_level=None,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA foreign_keys=ON")
        self._conn = conn
        await self._migrate()

    async def close(self) -> None:
        if self._conn is not None:
            conn = self._conn
            self._conn = None
            await asyncio.to_thread(conn.close)

    async def backup(self, dest: str | Path) -> None:
        """把当前库完整备份到 dest（SQLite 在线备份，包含 WAL 内容）。"""
        path = Path(dest)
        path.parent.mkdir(parents=True, exist_ok=True)

        def _do() -> None:
            target = sqlite3.connect(str(path))
            try:
                self.conn.backup(target)
            finally:
                target.close()

        await self._run(_do)

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("database is not connected")
        return self._conn

    # ------------------------------------------------------------------ 执行
    async def _run(self, fn):
        if self._in_tx:
            return await asyncio.to_thread(fn)
        async with self._lock:
            return await asyncio.to_thread(fn)

    async def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        def _do() -> int:
            cur = self.conn.execute(sql, tuple(params))
            return int(cur.lastrowid or 0)

        return await self._run(_do)

    async def executemany(self, sql: str, rows: Iterable[Sequence[Any]]) -> None:
        data = [tuple(row) for row in rows]

        def _do() -> None:
            self.conn.executemany(sql, data)

        await self._run(_do)

    async def fetchone(self, sql: str, params: Sequence[Any] = ()) -> dict[str, Any] | None:
        def _do() -> dict[str, Any] | None:
            row = self.conn.execute(sql, tuple(params)).fetchone()
            return dict(row) if row is not None else None

        return await self._run(_do)

    async def fetchall(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        def _do() -> list[dict[str, Any]]:
            rows = self.conn.execute(sql, tuple(params)).fetchall()
            return [dict(row) for row in rows]

        return await self._run(_do)

    async def scalar(self, sql: str, params: Sequence[Any] = (), default: Any = None) -> Any:
        row = await self.fetchone(sql, params)
        if not row:
            return default
        return next(iter(row.values()))

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[None]:
        if self._in_tx:
            raise RuntimeError("nested transaction is not supported")
        async with self._lock:
            self._in_tx = True
            try:
                await asyncio.to_thread(self.conn.execute, "BEGIN IMMEDIATE")
                yield
                await asyncio.to_thread(self.conn.execute, "COMMIT")
            except BaseException:
                await asyncio.to_thread(self.conn.execute, "ROLLBACK")
                raise
            finally:
                self._in_tx = False

    # ------------------------------------------------------------------ KV
    async def kv_get(self, bot_id: str, key: str, default: str | None = None) -> str | None:
        row = await self.fetchone(
            "SELECT value FROM kv WHERE bot_id=? AND key=?", (bot_id, key)
        )
        return default if row is None else row.get("value")

    async def kv_set(self, bot_id: str, key: str, value: str, now: int) -> None:
        await self.execute(
            "INSERT INTO kv(bot_id, key, value, updated_at) VALUES(?,?,?,?) "
            "ON CONFLICT(bot_id, key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (bot_id, key, value, now),
        )

    # ------------------------------------------------------------------ 迁移
    async def _migrate(self) -> None:
        version = await self.scalar("PRAGMA user_version", default=0) or 0
        await self._run(lambda: self.conn.executescript(DDL))
        await self._ensure_columns()
        await self._run(lambda: self.conn.executescript(DDL_INDEXES))
        if version < 2:
            await self._ensure_columns()
        try:
            await self._run(
                lambda: self.conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            )
        except sqlite3.Error:
            pass
        row = await self.fetchone("SELECT version FROM schema_version LIMIT 1")
        if row is None:
            await self.execute("INSERT INTO schema_version(version) VALUES(?)", (SCHEMA_VERSION,))
        else:
            await self.execute(
                "UPDATE schema_version SET version=? WHERE version<>?", (SCHEMA_VERSION, SCHEMA_VERSION)
            )

    async def _ensure_columns(self) -> None:
        wanted: dict[str, dict[str, str]] = {
            "events": {
                "kind": "TEXT NOT NULL DEFAULT 'episodic'",
                "storage_strength": "REAL NOT NULL DEFAULT 7.0",
                "retrievability": "REAL NOT NULL DEFAULT 1.0",
                "last_recalled_at": "INTEGER",
                "last_injected_at": "INTEGER",
                "merged_into": "INTEGER",
                "entities": "TEXT",
                "source_event_ids": "TEXT",
            },
            "facts": {
                "storage_strength": "REAL NOT NULL DEFAULT 30.0",
                "retrievability": "REAL NOT NULL DEFAULT 1.0",
                "last_recalled_at": "INTEGER",
                "valid_from": "INTEGER",
                "valid_to": "INTEGER",
                "superseded_by": "INTEGER",
                "key_fact": "INTEGER NOT NULL DEFAULT 0",
            },
            "distill_logs": {"gist_count": "INTEGER", "insight_count": "INTEGER"},
        }

        def _do() -> None:
            for table, columns in wanted.items():
                existing = {
                    row["name"]
                    for row in self.conn.execute(f"PRAGMA table_info({table})").fetchall()
                }
                if not existing:
                    continue
                for column, decl in columns.items():
                    if column in existing:
                        continue
                    try:
                        self.conn.execute(
                            f"ALTER TABLE {table} ADD COLUMN {column} {decl}"
                        )
                    except sqlite3.Error:
                        continue

        await self._run(_do)
