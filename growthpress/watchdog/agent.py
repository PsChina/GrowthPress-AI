"""watchdog 主流程: 周期扫各 state 停留时间, 超阈值 reset / alert / 升级 human_queue.

按 memory architecture.md / 行程守护 / 第 3 层 设计:
- 每 5min 扫一次
- state 卡 N min 触发动作 (reset 重投 / alert 不动)
- 同一 draft 干预 ≥3 次 → 强制 human_queue 防循环
- 单次扫描失败 try/except 包住, 不挂 task
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from ..db import Database

log = logging.getLogger("growthpress.watchdog")

# state → 超时阈值 + 动作.
# v0.2 新架构: 大脑是外部 claude CLI, daemon 不主动写文/改稿. 卡死统一 escalate
# 到 human_queue, 不 reset 重投 (没人接).
# pending_human 由 pending_watch_task 单独处理 (24h reminder + pending_long)
THRESHOLDS: dict[str, dict] = {
    # new / reviewing 新架构下 Claude 直接落 approved, 不应再出现. 历史数据 / 异常路径
    # 卡这两个 state → alert 仅 log, 等人工查 (强转 human_queue 可能误删数据).
    "new":        {"timeout_min": 60, "action": "alert"},
    "reviewing":  {"timeout_min": 60, "action": "alert"},
    # revising_dispatcher 60s 扫. 60min 还卡说明 claude subprocess 起不来 / 持续失败
    "revising":   {"timeout_min": 60, "action": "to_human_queue"},
    # m3 send-apv 应该即时. 30min 没动说明 SMTP 故障 / approver 失败 → alert 查日志
    "approved":   {"timeout_min": 30, "action": "alert"},
    # m4_pump 60s 扫. 30min 没出说明 publisher 卡 (xhs session 失效 / playwright 挂)
    "publishing": {"timeout_min": 30, "action": "to_human_queue"},
}

WATCHDOG_INTERVAL_SEC = 300       # 5 min
MAX_RETRIES_PER_DRAFT = 3         # 防循环: 单 draft 累计干预 ≥3 次直接 human_queue


async def watchdog_task(db: Database) -> None:
    """长生命周期 task. 5min 扫一次. orchestrator 启动它."""
    log.info(
        f"[watchdog] start, interval={WATCHDOG_INTERVAL_SEC}s, "
        f"watching {list(THRESHOLDS.keys())}"
    )
    while True:
        try:
            await _scan_once(db)
        except Exception as e:
            log.error(f"[watchdog] scan failed: {e!r}", exc_info=True)
        await asyncio.sleep(WATCHDOG_INTERVAL_SEC)


async def _scan_once(db: Database) -> None:
    """单次扫描. 遍历 THRESHOLDS 找超时 draft."""
    now = datetime.now(timezone.utc)
    handled_count = 0
    for state, cfg in THRESHOLDS.items():
        threshold_iso = (now - timedelta(minutes=cfg["timeout_min"])).isoformat()
        rows = await db.read(
            "SELECT id, state, updated_at FROM drafts WHERE state=? AND updated_at < ?",
            (state, threshold_iso),
        )
        for r in rows:
            await _handle_stuck(dict(r), cfg, db)
            handled_count += 1
    if handled_count:
        log.info(f"[watchdog] handled {handled_count} stuck drafts this scan")


async def _handle_stuck(draft: dict, cfg: dict, db: Database) -> None:
    """处理一个卡住的 draft.

    防循环: state_log 里同一 draft 的 by_agent='watchdog' 次数 ≥ MAX_RETRIES_PER_DRAFT
    时, 直接 human_queue, 不再 reset.
    """
    draft_id = draft["id"]
    state = draft["state"]

    retries = await db.read(
        "SELECT COUNT(*) AS n FROM state_log "
        "WHERE draft_id=? AND by_agent='watchdog'",
        (draft_id,),
    )
    retry_count = retries[0]["n"]

    if retry_count >= MAX_RETRIES_PER_DRAFT:
        log.warning(
            f"[watchdog] {draft_id} 已干预 {retry_count} 次仍卡 {state!r}, → human_queue"
        )
        await db.transition(
            draft_id, state, "human_queue", "watchdog",
            reason=f"卡 {state} 超时, watchdog 已干预 {retry_count} 次",
        )
        return

    action = cfg["action"]
    if action == "reset":
        await _reset(draft_id, state, cfg["to"], cfg["timeout_min"], retry_count, db)
    elif action == "alert":
        await _alert(draft_id, state, cfg["timeout_min"], retry_count, db)
    elif action == "to_human_queue":
        await _to_human_queue(draft_id, state, cfg["timeout_min"], retry_count, db)
    else:
        log.error(f"[watchdog] 未知 action {action!r} for state {state!r}")


async def _to_human_queue(
    draft_id: str, state: str, timeout_min: int, retry_count: int, db: Database,
) -> None:
    """强转 human_queue. 用在 revising / publishing 这类卡死无法自愈的 state."""
    ok = await db.transition(
        draft_id, state, "human_queue", "watchdog",
        reason=f"escalate: 卡 {state} 超 {timeout_min}min (干预 {retry_count + 1})",
    )
    if ok:
        log.warning(
            f"[watchdog] {draft_id} {state} → human_queue "
            f"(timeout escalate, 干预 {retry_count + 1})"
        )
    else:
        log.info(f"[watchdog] {draft_id} CAS 失败 (state 已被改), 跳过")


async def _reset(
    draft_id: str, state: str, to_state: str,
    timeout_min: int, retry_count: int, db: Database,
) -> None:
    """重投: 若 from==to, 刷新 updated_at + 写 state_log; 否则正常 CAS transition."""
    if state == to_state:
        # 同 state 自循环 (new → new): CAS 不会改 row, 手动 UPDATE + log
        now = datetime.now(timezone.utc).isoformat()
        await db.write(
            "UPDATE drafts SET updated_at=? WHERE id=? AND state=?",
            (now, draft_id, state),
        )
        await db.write(
            "INSERT INTO state_log "
            "(draft_id, from_state, to_state, by_agent, at, reason) "
            "VALUES (?, ?, ?, 'watchdog', ?, ?)",
            (draft_id, state, state, now,
             f"refresh: 卡 {state} 超 {timeout_min}min (干预 {retry_count + 1})"),
        )
        log.warning(
            f"[watchdog] {draft_id} 刷新 updated_at ({state} 自重投, "
            f"干预 {retry_count + 1}/{MAX_RETRIES_PER_DRAFT})"
        )
    else:
        ok = await db.transition(
            draft_id, state, to_state, "watchdog",
            reason=f"reset: 卡 {state} 超 {timeout_min}min (干预 {retry_count + 1})",
        )
        if ok:
            log.warning(
                f"[watchdog] {draft_id} {state} → {to_state} "
                f"(timeout reset, 干预 {retry_count + 1}/{MAX_RETRIES_PER_DRAFT})"
            )
        else:
            log.info(f"[watchdog] {draft_id} CAS 失败 (state 已被改), 跳过")


async def _alert(
    draft_id: str, state: str, timeout_min: int, retry_count: int, db: Database,
) -> None:
    """报警但不 reset (留人工干预). 仍记 state_log 标"已报警".

    日报邮件可统计 alert 次数 (state_log WHERE by_agent='watchdog' AND reason LIKE 'alert:%').
    """
    log.warning(
        f"[watchdog] ALERT: {draft_id} 卡 {state!r} 超 {timeout_min}min "
        f"(已报警 {retry_count} 次, 不 reset, 人工处理)"
    )
    now = datetime.now(timezone.utc).isoformat()
    await db.write(
        "INSERT INTO state_log "
        "(draft_id, from_state, to_state, by_agent, at, reason) "
        "VALUES (?, ?, ?, 'watchdog', ?, ?)",
        (draft_id, state, state, now,
         f"alert: 卡 {state} 超 {timeout_min}min (已报警 {retry_count + 1})"),
    )
