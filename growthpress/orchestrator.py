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
from .runners.email_chat_dispatcher import email_chat_dispatcher_run
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


_PID_FILE = Path(__file__).resolve().parent.parent / "data" / "daemon.pid"


def _scan_sibling_daemons() -> list[tuple[int, str]]:
    """ps 扫所有进程, 找其他 growthpress daemon (排除本进程 + uv run wrapper).

    比 PID file 鲁棒 — 即使用户 `rm -f daemon.pid` 删了锁文件, 老 daemon 仍
    被发现 (ps 不撒谎). 返回 [(pid, cmd 摘要)].

    匹配规则: command 含 '.venv/bin/growthpress' (python entry-point binary 路径,
    'uv run growthpress' 父 wrapper 不含此路径所以不会误伤).
    """
    import os
    import subprocess
    try:
        result = subprocess.run(
            ["ps", "-ax", "-o", "pid=,command="],
            capture_output=True, text=True, check=False, timeout=5,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        log.warning(f"[singleton] ps 扫描失败 ({e!r}), 跳过 sibling 检测 — "
                    "锁退化为 PID-file-only, 风险: 若你 rm -f daemon.pid 加重启,"
                    " 双 daemon 不会被拦住")
        return []

    me = os.getpid()
    siblings: list[tuple[int, str]] = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        try:
            pid = int(parts[0])
        except ValueError:
            continue
        if pid == me:
            continue
        cmd = parts[1]
        if ".venv/bin/growthpress" in cmd:
            siblings.append((pid, cmd[:150]))
    return siblings


def _acquire_single_instance_lock() -> int:
    """单实例锁: ps 进程扫 + PID file 双层防御. 已有活 daemon 拒绝启动.

    场景: 用户 / launchd 反复启动 daemon, 老进程因 IMAP hang 没退干净 → 堆积多
    进程抢同一邮箱 / 同一 publishing draft, 导致一篇被发 2 遍.

    锁的两层防御:
      1. ps -ax 扫 .venv/bin/growthpress 进程 — 真相在内核 process table, 即使
         用户 rm -f daemon.pid 删了锁文件, ps 仍能看到老进程
      2. PID file (info 用) — 方便 kill $(cat daemon.pid) 这种快捷写法, 但不是
         lock 决策的依据

    场景对照表:
      - 老 daemon 死透 + PID file 残留    → ps 扫无 sibling, 写新 PID, 启动 ✓
      - 老 daemon 死透 + PID file 已删    → ps 扫无 sibling, 写新 PID, 启动 ✓
      - 老 daemon 还活 + PID file 残留    → ps 扫到 sibling, refuse + 报错 ✗
      - 老 daemon 还活 + PID file 已删    → ps 扫到 sibling, refuse + 报错 ✗ ★
        (★ 是这次修的 bug — 之前依赖 PID file 存在性, 用户 rm -f 会绕过锁)

    返回: 当前进程 PID. 抛 SystemExit(3) 表示已有活实例.
    """
    import os

    siblings = _scan_sibling_daemons()
    if siblings:
        for pid, cmd in siblings:
            log.error(
                f"[singleton] 已有 daemon 实例运行 (pid={pid}). cmd: {cmd}\n"
                f"    如要重启, 先 `kill {pid}` (验证 `ps -p {pid}` 真死了) 再起.\n"
                f"    不要 `rm -f {_PID_FILE}` 绕过 — 那不能让旧进程死, 只会让\n"
                f"    新 daemon 起来后两个抢同一邮箱 / 重复发同一篇 (本 bug 的成因)."
            )
        raise SystemExit(3)

    # PID file 仅作 info / 便利 (kill $(cat ...) 用), 不是锁决策依据
    _PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    _PID_FILE.write_text(str(os.getpid()))
    log.info(f"[singleton] 获取锁: pid={os.getpid()} → {_PID_FILE} "
             f"(ps 扫无 sibling)")
    return os.getpid()


def _release_single_instance_lock() -> None:
    """退出时清 PID file. 失败仅 log."""
    try:
        if _PID_FILE.exists():
            _PID_FILE.unlink()
            log.info(f"[singleton] 释放锁: {_PID_FILE}")
    except OSError as e:
        log.warning(f"[singleton] 清 PID file 失败 (忽略): {e!r}")


async def main_async() -> None:
    # 单实例锁 (防 daemon 假死时反复启动堆积进程, 抢同一邮箱 → 全部 hang)
    _acquire_single_instance_lock()

    # m4 真发开关 — 默认 dry_run=True (安全). 设 DAEMON_PUBLISHER_DRY_RUN=false 真发到平台.
    import os
    dry_run_str = os.environ.get("DAEMON_PUBLISHER_DRY_RUN", "true").lower()
    publisher_dry_run = dry_run_str not in ("false", "0", "no", "off")

    log.info(
        f"GrowthPress AI v{__version__} 启动, db={DB_PATH}, "
        f"publisher dry_run={publisher_dry_run} "
        f"(env DAEMON_PUBLISHER_DRY_RUN={dry_run_str!r})"
    )
    async with AsyncExitStack() as stack:
        db = await stack.enter_async_context(Database.open(DB_PATH))
        await resume_pending(db)

        stop = asyncio.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            asyncio.get_running_loop().add_signal_handler(sig, stop.set)

        try:
            stack.callback(_release_single_instance_lock)
            async with asyncio.TaskGroup() as tg:
                tg.create_task(db.writer_loop(), name="db_writer")
                tg.create_task(m3_pump_runs(db), name="m3_pump")
                tg.create_task(m4_pump_runs(db, dry_run=publisher_dry_run), name="m4_pump")
                tg.create_task(imap_poller_run(db), name="imap_poller")
                tg.create_task(pending_watch(db), name="pending_watch")
                tg.create_task(watchdog_task(db), name="watchdog")
                tg.create_task(daily_digest_task(db), name="daily_digest")
                # 新: 派工给 claude CLI 改稿 (用户 APV 回 "改 X" 后)
                tg.create_task(revising_dispatcher_run(db), name="revising_dispatcher")
                # 新: 派工给 claude CLI 发新内容 (用户邮件 subject 含"发"/"发一篇"关键字后)
                tg.create_task(email_chat_dispatcher_run(db), name="email_chat_dispatcher")
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

    # stdout/stderr 行缓冲 — daemon 跑在 launchd / pipe 下默认 block-buffer,
    # log 不及时刷会让"看不见错误"看起来像 silent failure. 强制 line-buffered.
    try:
        sys.stdout.reconfigure(line_buffering=True)
        sys.stderr.reconfigure(line_buffering=True)
    except Exception:
        pass

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
