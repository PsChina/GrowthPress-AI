"""Daemon 主入口 — TaskGroup 编排所有长生命周期任务.

新架构: GrowthPress 不再内置 LLM 调用 (m1 scout_writer / m2 reviewer 已删).
"大脑"由外部 claude CLI 担任. daemon 只跑:
- db.writer_loop      — SQLite 单 writer
- imap_poller         — 拉新邮件 (触发 claude CLI 是 mailbox handler 的事)
- m3_pump             — 周期扫 approved 发 APV 邮件
- m4_pump             — 周期扫 publishing 调 publisher 真发
- pending_watch       — 扫 APV 超时
- watchdog            — 卡 state 兜底
- daily_digest        — 每日汇总邮件

调研 / 找图 / 写文 都由 claude CLI 在外部触发时跑 (cli.py + skill).
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from contextlib import AsyncExitStack
from pathlib import Path

from . import __version__
from .approver import send_approval
from .daily_digest import daily_digest_task
from .db import Database
from .mailbox import imap_poller_run
from .publisher.m4_pump import m4_pump_runs
from .runners.revising_dispatcher import revising_dispatcher_run
from .watchdog import watchdog_task

log = logging.getLogger("growthpress")

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "runs.db"


async def m3_pump_runs(db: Database) -> None:
    """周期扫 drafts.state='approved' 调 m3 send_approval (发 APV 邮件).

    简单轮询模式 (60s), 失败 try/except 包住不挂 task. m3 send_approval 内部:
    - 用 db.transition CAS 锁 state approved → pending_human (失败说明 state 已变, 跳过)
    - INSERT approvals state='pending' expires_at=now+24h
    - SMTP 发邮件; 失败回滚 (DELETE approvals + state 回 approved 等下轮重试)
    """
    log.info("[m3_pump] start, interval=60s")
    while True:
        try:
            rows = await db.read(
                "SELECT id FROM drafts WHERE state='approved' ORDER BY updated_at LIMIT 10"
            )
            for r in rows:
                draft_id = r["id"]
                try:
                    pub_id = await send_approval(draft_id, db)
                    if pub_id:
                        log.info(f"[m3_pump] APV sent: draft={draft_id} pub={pub_id}")
                except Exception as e:
                    log.error(
                        f"[m3_pump] send_approval failed for {draft_id}: {e!r}",
                        exc_info=True,
                    )
        except Exception as e:
            log.error(f"[m3_pump] scan failed: {e!r}", exc_info=True)
        await asyncio.sleep(60)


async def pending_watch(db: Database) -> None:
    """W3 接: 每 5min 扫 approvals.state=pending 超时 → reminder / pending_long."""
    while True:
        log.debug("[pending_watch] tick (W3 stub, 无动作)")
        await asyncio.sleep(300)


async def resume_pending(db: Database) -> None:
    """启动时崩溃恢复: 扫 publishing / pending_human, 重启对应 task."""
    rows = await db.read(
        "SELECT id, state FROM drafts WHERE state IN ('publishing', 'pending_human')"
    )
    if rows:
        log.info(f"[resume] 发现 {len(rows)} 个 in-flight drafts: "
                 f"{[(r['id'], r['state']) for r in rows]}")
    else:
        log.info("[resume] 无 in-flight drafts")


async def main_async() -> None:
    log.info(f"GrowthPress AI v{__version__} 启动, db={DB_PATH}")
    async with AsyncExitStack() as stack:
        db = await stack.enter_async_context(Database.open(DB_PATH))
        await resume_pending(db)

        stop = asyncio.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            asyncio.get_running_loop().add_signal_handler(sig, stop.set)

        try:
            async with asyncio.TaskGroup() as tg:
                tg.create_task(db.writer_loop(), name="db_writer")
                tg.create_task(m3_pump_runs(db), name="m3_pump")
                tg.create_task(m4_pump_runs(db, dry_run=True), name="m4_pump")
                tg.create_task(imap_poller_run(db), name="imap_poller")
                tg.create_task(pending_watch(db), name="pending_watch")
                tg.create_task(watchdog_task(db), name="watchdog")
                tg.create_task(daily_digest_task(db), name="daily_digest")
                # 新: 派工给 claude CLI 改稿 (用户 APV 回 "改 X" 后)
                tg.create_task(revising_dispatcher_run(db), name="revising_dispatcher")
                tg.create_task(_wait_stop(stop, tg), name="stop_watcher")
        except* asyncio.CancelledError:
            pass  # TaskGroup 收到 stop 信号正常退出


async def _wait_stop(stop: asyncio.Event, tg: asyncio.TaskGroup) -> None:
    await stop.wait()
    log.info("收到 stop 信号, 取消所有 task")
    raise asyncio.CancelledError


def cli_main() -> int:
    ap = argparse.ArgumentParser(prog="growthpress", description="GrowthPress AI daemon")
    ap.add_argument("--version", action="version", version=f"GrowthPress AI {__version__}")
    ap.add_argument("--log-level", default="INFO",
                    choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = ap.parse_args()

    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    # 启动前 preflight: tty 模式询问跑 setup, 非 tty 仅 log
    from .preflight import maybe_interactive, warn
    maybe_interactive(source="daemon")
    warn(source="daemon")
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(cli_main())
