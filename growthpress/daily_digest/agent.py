"""日报: 每天 UTC 0:00 (北京 8:00) 发 24h 统计邮件给 notify_to.

按 memory architecture / 行程守护 / 第 5 层 设计.

统计内容:
  - 昨日发布数 (按 platform 分组, 成功/失败)
  - 当前在制 drafts 各 state 数量
  - watchdog 24h 干预次数
  - LLM 调用统计 (flash/pro 各几次, 成本可在 W3 补)
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage

import aiosmtplib

from ..core.settings import get_settings
from ..db import Database

log = logging.getLogger("growthpress.daily_digest")

DAILY_HOUR_UTC = 0   # UTC 0:00 = 北京 8:00


async def daily_digest_task(db: Database) -> None:
    """长生命周期 task. sleep 到下一个 DAILY_HOUR_UTC 触发, 失败 try/except 不挂."""
    s = get_settings()
    if not s.smtp_user or not s.notify_to:
        log.warning("[daily_digest] SMTP / notify_to 未配置, task exit")
        return
    log.info(
        f"[daily_digest] start, daily at UTC {DAILY_HOUR_UTC:02d}:00 → {s.notify_to}"
    )
    while True:
        next_run = _next_run_time()
        sleep_sec = (next_run - datetime.now(timezone.utc)).total_seconds()
        log.info(
            f"[daily_digest] sleep {sleep_sec / 3600:.1f}h to {next_run.isoformat()}"
        )
        await asyncio.sleep(sleep_sec)
        try:
            await _send_digest(db)
        except Exception as e:
            log.error(f"[daily_digest] failed: {e!r}", exc_info=True)


def _next_run_time() -> datetime:
    now = datetime.now(timezone.utc)
    today = now.replace(hour=DAILY_HOUR_UTC, minute=0, second=0, microsecond=0)
    return today + timedelta(days=1) if now >= today else today


async def _send_digest(db: Database) -> None:
    s = get_settings()
    since = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    today = datetime.now(timezone.utc).date().isoformat()

    pubs = await db.read(
        "SELECT platform, COUNT(*) AS n, "
        "SUM(CASE WHEN state='published' THEN 1 ELSE 0 END) AS ok, "
        "SUM(CASE WHEN state='failed' THEN 1 ELSE 0 END) AS fail "
        "FROM publications "
        "WHERE COALESCE(published_at, retract_window_until) >= ? GROUP BY platform",
        (since,),
    )
    pending = await db.read(
        "SELECT state, COUNT(*) AS n FROM drafts "
        "WHERE state IN ('new','reviewing','revising','approved','publishing',"
        "'pending_human','pending_long','human_queue') GROUP BY state",
    )
    watchdog_n = (
        await db.read(
            "SELECT COUNT(*) AS n FROM state_log "
            "WHERE by_agent='watchdog' AND at >= ?",
            (since,),
        )
    )[0]["n"]
    llm = await db.read(
        "SELECT model, COUNT(*) AS n, "
        "SUM(COALESCE(input_tokens,0)) AS itk, SUM(COALESCE(output_tokens,0)) AS otk, "
        "SUM(CASE WHEN success THEN 0 ELSE 1 END) AS fails "
        "FROM llm_calls WHERE at >= ? GROUP BY model",
        (since,),
    )

    lines = [
        f"# GrowthPress 日报 ({today})",
        f"  统计窗口: 过去 24h (since {since})",
        "",
        "## 发布",
    ]
    if pubs:
        for p in pubs:
            lines.append(
                f"  {p['platform']}: 共 {p['n']} 次 "
                f"(成功 {p['ok']}, 失败 {p['fail']})"
            )
    else:
        lines.append("  (无)")

    lines.extend(["", "## 在制 drafts"])
    if pending:
        for p in pending:
            lines.append(f"  {p['state']}: {p['n']}")
    else:
        lines.append("  (无)")

    lines.extend(["", f"## watchdog 干预: {watchdog_n} 次"])

    lines.extend(["", "## LLM 调用"])
    if llm:
        for c in llm:
            lines.append(
                f"  {c['model']}: {c['n']} 次 (失败 {c['fails']}), "
                f"input={c['itk']} tok, output={c['otk']} tok"
            )
    else:
        lines.append("  (无)")

    lines.extend(["", "—", "GrowthPress AI 自动日报. 异常请检查 logs."])

    body = "\n".join(lines)
    subject = f"[DAILY-{today}] GrowthPress 日报"

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = s.smtp_user
    msg["To"] = s.notify_to
    msg.set_content(body)

    await aiosmtplib.send(
        msg,
        hostname=s.smtp_host,
        port=s.smtp_port,
        username=s.smtp_user,
        password=s.smtp_pass.get_secret_value(),
        start_tls=True,
    )
    log.info(f"[daily_digest] sent {subject!r} to {s.notify_to}")
