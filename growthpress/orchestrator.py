"""Daemon 主入口 — TaskGroup 编排所有长生命周期任务.

按 memory architecture.md L117-L139 设计:
- scheduler_task: apscheduler / sleep loop, 每天 N 次触发新文
- imap_poller_task: 每 30s 扫未读邮件
- pending_watch: 每 5min 扫超时
- db.writer_loop: SQLite 单 writer

W1 阶段: scheduler/imap/pending_watch 是 stub (打 log + sleep), 让框架先跑起来.
W2 接 Publisher 时填 scheduler.
W3 接邮件时填 imap_poller + pending_watch.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from contextlib import AsyncExitStack
from pathlib import Path

from . import __version__, scout_writer
from .db import Database
from .scout_writer.topics import load_topics_config, pick_next_topic

log = logging.getLogger("growthpress")

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "runs.db"


async def schedule_runs(db: Database) -> None:
    """按 config/topics.yaml 间隔触发 m1 scout_writer.

    默认 schedule.enabled=false (防止 daemon 启动就烧 LLM), task 直接退出.
    enabled=true 时按 interval_minutes 节奏跑, 每次选最久没跑过的 topic.

    单次 m1 调用失败不挂 task (try/except 包住), 等下个 interval 再试.
    """
    try:
        cfg = load_topics_config()
    except ValueError as e:
        log.error(f"[scheduler] config 加载失败: {e}")
        log.error("[scheduler] task exit, 修复 config 后重启 daemon")
        return
    if not cfg.schedule.enabled:
        log.info(
            "[scheduler] disabled (config/topics.yaml schedule.enabled=false), task exit"
        )
        return
    if not cfg.topics:
        log.warning("[scheduler] enabled 但 topics 为空, task exit")
        return

    log.info(
        f"[scheduler] enabled, interval={cfg.schedule.interval_minutes}min, "
        f"{len(cfg.topics)} topics: {cfg.topics[:3]}{'...' if len(cfg.topics) > 3 else ''}"
    )
    while True:
        try:
            topic = await pick_next_topic(cfg.topics, db)
            if topic is None:
                log.warning("[scheduler] pick_next_topic 返回 None, sleep one interval")
            else:
                log.info(f"[scheduler] triggering m1: topic={topic!r}")
                draft_id, _ = await scout_writer.run(topic, db=db)
                log.info(f"[scheduler] m1 ok: draft_id={draft_id}")
        except Exception as e:
            log.error(f"[scheduler] m1 failed: {e!r}", exc_info=True)
        await asyncio.sleep(cfg.schedule.interval_minutes * 60)


async def imap_poller_run(db: Database) -> None:
    """W3 接: aioimaplib 每 30s 扫 INBOX, APV-*/PUB-* 分发."""
    while True:
        log.debug("[imap] tick (W3 stub, 无动作)")
        await asyncio.sleep(30)


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
                tg.create_task(schedule_runs(db), name="scheduler")
                tg.create_task(imap_poller_run(db), name="imap_poller")
                tg.create_task(pending_watch(db), name="pending_watch")
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
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(cli_main())
