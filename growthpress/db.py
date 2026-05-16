"""SQLite + WAL + 单 writer pattern.

SQLite 本身写锁 → 开 WAL 后并发读 OK, 写仍串行 → 用 queue + 单 writer task 保护.
所有写必须经 db.write(), 直接 conn.execute(INSERT/UPDATE) 是 bug.

Schema 见 [[project-content-bot-skeleton]] L64-L132.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS drafts (
    id           TEXT PRIMARY KEY,
    topic        TEXT NOT NULL,
    title        TEXT,
    body_md      TEXT,
    summary      TEXT,
    sources      TEXT,                    -- JSON array
    state        TEXT NOT NULL,           -- new | reviewing | revising | approved |
                                          -- publishing | published | archived |
                                          -- pending_human | pending_long | human_queue
    revise_count INT DEFAULT 0,
    created_at   TIMESTAMP NOT NULL,
    updated_at   TIMESTAMP NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_drafts_state ON drafts(state);

CREATE TABLE IF NOT EXISTS reviews (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    draft_id        TEXT NOT NULL,
    round           INT NOT NULL,         -- 第 N 轮 (≤2)
    pass_           BOOLEAN NOT NULL,     -- 'pass' 是 Python 关键字, 后缀 _
    score           INT,
    issues          TEXT,                 -- JSON array
    suggested_edits TEXT,                 -- JSON array
    reviewed_at     TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS approvals (
    id              TEXT PRIMARY KEY,     -- pub_id (uuid 前 6 位)
    draft_id        TEXT NOT NULL,
    sent_at         TIMESTAMP NOT NULL,
    expires_at      TIMESTAMP NOT NULL,
    state           TEXT NOT NULL,        -- pending | approved | rejected | dropped | pending_long
    platforms       TEXT,                 -- approve 时回填 csv
    feedback        TEXT,                 -- reject 时的 issues
    reminder_count  INT DEFAULT 0,
    decided_at      TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_approvals_state ON approvals(state);

CREATE TABLE IF NOT EXISTS publications (
    id                   TEXT PRIMARY KEY,
    draft_id             TEXT NOT NULL,
    platform             TEXT NOT NULL,
    post_id              TEXT,            -- 平台返回的文章 ID (撤销用)
    url                  TEXT,
    state                TEXT NOT NULL,   -- published | retracted | failed
    published_at         TIMESTAMP,
    retract_window_until TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_publications_state ON publications(state);

CREATE TABLE IF NOT EXISTS retractions (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    publication_id TEXT NOT NULL,
    requested_at   TIMESTAMP NOT NULL,
    state          TEXT NOT NULL,         -- requested | done | failed
    error          TEXT
);
"""

PRAGMAS = [
    "PRAGMA journal_mode=WAL",
    "PRAGMA synchronous=NORMAL",
    "PRAGMA busy_timeout=5000",
    "PRAGMA foreign_keys=ON",
]


class Database:
    def __init__(self, conn: aiosqlite.Connection):
        self.conn = conn
        self.write_q: asyncio.Queue = asyncio.Queue()
        self._writer_task: asyncio.Task | None = None

    @classmethod
    @asynccontextmanager
    async def open(cls, path: str | Path):
        """async with Database.open(path) as db: ...

        启动 PRAGMA + schema, 关闭时 close 连接 (但 writer_loop 由 orchestrator
        放进 TaskGroup 管控生命周期, 不在这里 start)."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(path)
        conn.row_factory = aiosqlite.Row
        try:
            for pragma in PRAGMAS:
                await conn.execute(pragma)
            await conn.executescript(SCHEMA)
            await conn.commit()
            yield cls(conn)
        finally:
            await conn.close()

    async def writer_loop(self) -> None:
        """单 writer 串行落库. 由 orchestrator 的 TaskGroup create_task 启动."""
        while True:
            sql, params, fut = await self.write_q.get()
            try:
                cur = await self.conn.execute(sql, params)
                await self.conn.commit()
                fut.set_result(cur.lastrowid)
            except Exception as e:
                fut.set_exception(e)

    async def write(self, sql: str, params: tuple = ()) -> int:
        """串行写, 返回 lastrowid."""
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        await self.write_q.put((sql, params, fut))
        return await fut

    async def read(self, sql: str, params: tuple = ()) -> list[aiosqlite.Row]:
        """并发读 (WAL 模式下读不阻塞)."""
        async with self.conn.execute(sql, params) as cur:
            return await cur.fetchall()
