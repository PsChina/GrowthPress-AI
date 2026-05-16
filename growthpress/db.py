"""SQLite + WAL + 单 writer pattern.

SQLite 本身写锁 → 开 WAL 后并发读 OK, 写仍串行 → 用 queue + 单 writer task 保护.
所有写必须经 db.write() / db.transition(), 直接 conn.execute(INSERT/UPDATE) 是 bug.

Schema 见 [[project-content-bot-skeleton]] L64-L132.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
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
    media        TEXT DEFAULT '[]',       -- JSON array of LOCAL image paths.
                                          -- m1 在生成阶段调 web_search 找 URL → 立刻下载到
                                          -- data/images/<draft_id>/ → 表里存本地路径.
                                          -- m4 publisher 直接用, 不再下载 (素材一旦入库就稳).
                                          -- markdown body 里不嵌图 URL, 避免图文混排出错.
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

-- LLM 调用统计 (经济性追踪, 日报用)
CREATE TABLE IF NOT EXISTS llm_calls (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    draft_id      TEXT,
    task          TEXT NOT NULL,        -- scout_writing / review_compliance / ...
    model         TEXT NOT NULL,        -- flash / pro / claude (逻辑名, 非具体模型 ID)
    model_id      TEXT NOT NULL,        -- 实际模型 ID (deepseek-v4-flash[1m] 等)
    input_tokens  INT,
    output_tokens INT,
    cost_usd      REAL,                 -- 按价目表估算, NULL 表示未估
    duration_ms   INT,
    success       BOOLEAN NOT NULL,
    upgraded_from TEXT,                 -- 'flash' 若这次是升级后调用
    error         TEXT,                 -- success=false 时的错误概要
    at            TIMESTAMP NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_llm_calls_draft ON llm_calls(draft_id);
CREATE INDEX IF NOT EXISTS idx_llm_calls_at    ON llm_calls(at);

-- 行程守护第 4 层: 每次 state 变迁记一条, pending_long / debug 用
CREATE TABLE IF NOT EXISTS state_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    draft_id   TEXT NOT NULL,
    from_state TEXT,                      -- NULL 表示初始 insert
    to_state   TEXT NOT NULL,
    by_agent   TEXT NOT NULL,             -- m1/m2/m3/m4/m5/scheduler/watchdog/human
    at         TIMESTAMP NOT NULL,
    reason     TEXT
);
CREATE INDEX IF NOT EXISTS idx_state_log_draft ON state_log(draft_id, at);

-- email-chat 队列: 用户邮件触发 claude subprocess 的入口
-- intent='post' (subject 含"发"/"发一篇" 等关键字) → claude 走 growthpress-post skill 全套
-- intent='chat' (其他) → MVP 不处理, 留扩展
CREATE TABLE IF NOT EXISTS email_intake (
    id            TEXT PRIMARY KEY,        -- uuid 前 8 位
    message_id    TEXT UNIQUE,             -- 防同邮件被 IMAP poller 重拉重跑
    sender        TEXT NOT NULL,           -- RFC822 from
    subject       TEXT NOT NULL,
    body_text     TEXT NOT NULL,
    intent        TEXT NOT NULL,           -- post | chat
    state         TEXT NOT NULL,           -- pending | processing | done | failed
    claude_run_id TEXT,                    -- 起 claude subprocess 后回填
    draft_id      TEXT,                    -- claude 创建出的 draft id (intent=post)
    error         TEXT,
    created_at    TIMESTAMP NOT NULL,
    updated_at    TIMESTAMP NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_email_intake_state ON email_intake(state, created_at);
"""

PRAGMAS = [
    "PRAGMA journal_mode=WAL",
    "PRAGMA synchronous=NORMAL",
    "PRAGMA busy_timeout=5000",
    "PRAGMA foreign_keys=ON",
]


async def _migrate(conn: aiosqlite.Connection) -> None:
    """轻量 migration: SCHEMA 用 CREATE TABLE IF NOT EXISTS, 不会改既存表的列.

    新增列时, 这里 PRAGMA 检查 + ALTER. 不动数据, 旧行的新列拿默认值.
    """
    cur = await conn.execute("PRAGMA table_info(drafts)")
    cols = {row[1] for row in await cur.fetchall()}
    if "media" not in cols:
        await conn.execute(
            "ALTER TABLE drafts ADD COLUMN media TEXT DEFAULT '[]'"
        )


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
            await _migrate(conn)
            await conn.commit()
            yield cls(conn)
        finally:
            await conn.close()

    async def writer_loop(self) -> None:
        """单 writer 串行落库. 由 orchestrator 的 TaskGroup create_task 启动.

        future 填回 dict {lastrowid, rowcount}, 高层 helper 各取所需:
          - write()      → lastrowid (兼容旧调用)
          - transition() → rowcount (CAS UPDATE 判断锁是否成功)
        """
        while True:
            sql, params, fut = await self.write_q.get()
            try:
                cur = await self.conn.execute(sql, params)
                await self.conn.commit()
                fut.set_result({"lastrowid": cur.lastrowid, "rowcount": cur.rowcount})
            except Exception as e:
                fut.set_exception(e)

    async def _exec(self, sql: str, params: tuple = ()) -> dict:
        """内部: 进 queue 执行, 返回 {lastrowid, rowcount}."""
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        await self.write_q.put((sql, params, fut))
        return await fut

    async def write(self, sql: str, params: tuple = ()) -> int:
        """串行写, 返回 lastrowid (INSERT 用)."""
        result = await self._exec(sql, params)
        return result["lastrowid"]

    async def read(self, sql: str, params: tuple = ()) -> list[aiosqlite.Row]:
        """并发读 (WAL 模式下读不阻塞)."""
        async with self.conn.execute(sql, params) as cur:
            return await cur.fetchall()

    async def transition(
        self,
        draft_id: str,
        from_state: str,
        to_state: str,
        by_agent: str,
        reason: str | None = None,
    ) -> bool:
        """state 乐观锁变迁 + 写 state_log. 返回 True 表示成功 (锁住了 from_state).

        原子 CAS: `UPDATE drafts SET state=? WHERE id=? AND state=?`, rowcount=1
        表示锁到了, rowcount=0 表示 draft 不存在或 state 已被别人改过.

        架构 memory 'architecture.md / 行程守护 / 4 层 state_log' 设计.
        """
        now = datetime.now(timezone.utc).isoformat()

        result = await self._exec(
            "UPDATE drafts SET state=?, updated_at=? WHERE id=? AND state=?",
            (to_state, now, draft_id, from_state),
        )
        if result["rowcount"] != 1:
            return False

        await self.write(
            "INSERT INTO state_log "
            "(draft_id, from_state, to_state, by_agent, at, reason) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (draft_id, from_state, to_state, by_agent, now, reason),
        )
        return True

    async def log_initial_state(
        self,
        draft_id: str,
        to_state: str,
        by_agent: str,
        reason: str | None = None,
    ) -> None:
        """初始 INSERT drafts 后调用, 记 state_log (from_state=NULL).

        不用 transition() 因为没有 from_state 可 CAS, 单独走这个 helper.
        """
        now = datetime.now(timezone.utc).isoformat()
        await self.write(
            "INSERT INTO state_log "
            "(draft_id, from_state, to_state, by_agent, at, reason) "
            "VALUES (?, NULL, ?, ?, ?, ?)",
            (draft_id, to_state, by_agent, now, reason),
        )
