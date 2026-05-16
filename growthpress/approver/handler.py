"""m3 approver 回信处理: 由 m5 mailbox dispatcher 调用.

入口:
    await handle_approval_reply(
        {"subject": ..., "body_text": ..., "message_id": ..., "from_addr": ...},
        db,
    )

流程:
  1. 从 subject 抽 pub_id (Re: [APV-abc123] ... → abc123)
  2. 查 approvals 行, 校验 state=='pending' 且未过期
  3. parse_reply(body_text) → Action
  4. 按 Action 类型 dispatch:
     - Approve(platforms)  → approvals state=approved, drafts pending_human → publishing
     - Reject(feedback)    → approvals state=rejected, drafts pending_human → revising,
                             revise_count++
     - Drop()              → approvals state=dropped, drafts pending_human → archived
     - Unknown()           → 回 "未识别" 提示邮件, 不改 state
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from email.message import EmailMessage
from typing import Any

import aiosmtplib

from ..core.settings import get_settings
from ..db import Database
from .email_template import build_unknown_body, build_unknown_subject
from .reply_parser import parse_reply
from .schemas import Approve, Drop, Reject, Unknown

log = logging.getLogger("growthpress.approver")

# 从 subject 抽 pub_id. 兼容:
#   "[APV-7f3a2b] 待确认: ..."
#   "Re: [APV-7f3a2b] 待确认: ..."
#   "Re: Re: [APV-7f3a2b] ..."
_PUB_ID_RE = re.compile(r"\[APV-([0-9a-fA-F]{4,8})\]")


def extract_pub_id(subject: str) -> str | None:
    """从邮件 subject 抽 pub_id. 失败返 None.

    导出为顶层 helper, m5 dispatcher 在 routing 之前可以先用这个识别归属.
    """
    if not subject:
        return None
    m = _PUB_ID_RE.search(subject)
    return m.group(1).lower() if m else None


async def _send_unknown_reply(
    *,
    pub_id: str,
    raw: str,
    original_subject: str,
    to_addr: str,
) -> None:
    """回一封 Unknown 提示邮件. 失败只 log, 不抛 (本身就是辅助提示)."""
    s = get_settings()
    if not s.smtp_user:
        log.warning("[m3] SMTP 未配置, 跳过 Unknown 提示邮件")
        return

    msg = EmailMessage()
    msg["From"] = s.smtp_user
    msg["To"] = to_addr
    msg["Subject"] = build_unknown_subject(pub_id, original_subject)
    msg.set_content(build_unknown_body(pub_id=pub_id, raw=raw), charset="utf-8")
    try:
        from ..shared.smtp import smtp_send
        await smtp_send(msg)
        log.info(f"[m3] sent Unknown reply pub_id={pub_id} to={to_addr}")
    except Exception as e:
        log.warning(f"[m3] Unknown 提示邮件发送失败 pub_id={pub_id}: {e!r}")


async def _handle_approve(
    db: Database, pub_id: str, draft_id: str, action: Approve
) -> None:
    """Approve dispatch: 更新 approvals + 推进 drafts 到 publishing."""
    now = datetime.now(timezone.utc).isoformat()
    await db.write(
        "UPDATE approvals SET state='approved', platforms=?, decided_at=? WHERE id=?",
        (action.platforms, now, pub_id),
    )
    ok = await db.transition(
        draft_id, "pending_human", "publishing", "m3",
        reason=f"approve platforms={action.platforms}",
    )
    if not ok:
        log.warning(
            f"[m3] approve transition 失败 draft={draft_id} (不在 pending_human, "
            "可能已超时被改 / 已被其他回信处理), approvals 已标 approved"
        )
    else:
        log.info(f"[m3] approve pub_id={pub_id} draft={draft_id} platforms={action.platforms}")


async def _handle_reject(
    db: Database, pub_id: str, draft_id: str, action: Reject
) -> None:
    """Reject dispatch: 更新 approvals + 退回 revising + revise_count++."""
    now = datetime.now(timezone.utc).isoformat()
    await db.write(
        "UPDATE approvals SET state='rejected', feedback=?, decided_at=? WHERE id=?",
        (action.feedback, now, pub_id),
    )
    ok = await db.transition(
        draft_id, "pending_human", "revising", "m3",
        reason=f"reject feedback={action.feedback[:80]}",
    )
    if not ok:
        log.warning(
            f"[m3] reject transition 失败 draft={draft_id} (不在 pending_human)"
        )
        return
    # revise_count++  (单独 UPDATE, 不影响 state CAS).
    # m2 用 revise_count 判断是否超 2 轮 → human_queue.
    await db.write(
        "UPDATE drafts SET revise_count = revise_count + 1, updated_at=? WHERE id=?",
        (now, draft_id),
    )
    log.info(
        f"[m3] reject pub_id={pub_id} draft={draft_id} "
        f"feedback={action.feedback[:60]!r}"
    )


async def _handle_drop(
    db: Database, pub_id: str, draft_id: str
) -> None:
    """Drop dispatch: 更新 approvals + 归档."""
    now = datetime.now(timezone.utc).isoformat()
    await db.write(
        "UPDATE approvals SET state='dropped', decided_at=? WHERE id=?",
        (now, pub_id),
    )
    ok = await db.transition(
        draft_id, "pending_human", "archived", "m3",
        reason="user drop",
    )
    if not ok:
        log.warning(f"[m3] drop transition 失败 draft={draft_id} (不在 pending_human)")
    else:
        log.info(f"[m3] drop pub_id={pub_id} draft={draft_id}")


async def handle_approval_reply(
    message: dict[str, Any], db: Database
) -> None:
    """处理 APV 邮件回信. 由 m5 mailbox 在识别到 [APV-xxx] subject 后调用.

    Args:
        message: dict, 含 keys:
            - subject: str (原邮件 subject, e.g. "Re: [APV-7f3a2b] ...")
            - body_text: str (plain text body)
            - message_id: str (RFC 5322 Message-ID, 留 audit)
            - from_addr: str (回信人邮箱, 用于回 Unknown 提示)
        db: Database 实例.

    无返回值. 异常情况只 log 不抛 (m5 dispatcher 期望幂等无副作用):
      - subject 无 pub_id → log warning, 跳过 (m5 派错了)
      - approvals 行不存在 → log warning, 跳过 (可能 db 不一致 / 旧邮件)
      - approvals state != 'pending' → log info, 跳过 (重复回信 / 已超时)
      - 过期 → log warning, 跳过 (留给 watchdog 处理 pending_long)
    """
    subject = message.get("subject", "")
    body_text = message.get("body_text", "")
    from_addr = message.get("from_addr", "") or get_settings().notify_to

    # 1. 抽 pub_id
    pub_id = extract_pub_id(subject)
    if not pub_id:
        log.warning(f"[m3] subject 抽不到 pub_id, 跳过: {subject!r}")
        return

    # 2. 查 approvals
    rows = await db.read("SELECT * FROM approvals WHERE id=?", (pub_id,))
    if not rows:
        log.warning(f"[m3] approvals pub_id={pub_id} 不存在, 跳过")
        return
    row = dict(rows[0])
    draft_id = row["draft_id"]
    state = row["state"]

    if state != "pending":
        log.info(
            f"[m3] approvals pub_id={pub_id} state={state!r} (非 pending), 跳过 "
            "(可能重复回信 / 已处理)"
        )
        return

    # 3. 过期检查 — expires_at < now 则不处理 (留给 watchdog 转 pending_long)
    try:
        expires_at = datetime.fromisoformat(row["expires_at"])
        # 兼容 naive / aware (db 存的是 isoformat, 应该带 tz)
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at < datetime.now(timezone.utc):
            log.warning(
                f"[m3] approvals pub_id={pub_id} 已过期 (expires_at={row['expires_at']}), "
                "跳过 (watchdog 会转 pending_long)"
            )
            return
    except (ValueError, TypeError) as e:
        log.warning(f"[m3] expires_at 解析失败 pub_id={pub_id}: {e!r}, 继续处理")

    # 4. parse reply
    action = parse_reply(body_text)
    log.info(f"[m3] reply pub_id={pub_id} action={type(action).__name__}")
    # Unknown 时把 raw body 前 500 字 dump 到 log, 方便定位是哪种客户端 / 哪种引用前缀
    # 没被 parser 剥掉. body_text 可能含敏感信息 (邮件正文), 但 daemon log 是本地文件,
    # 不外泄. 调试完可以删. 用 repr 保留转义字符 (\n / \r / \xa0 等).
    from .schemas import Unknown
    if isinstance(action, Unknown):
        log.warning(
            f"[m3] reply pub_id={pub_id} Unknown 时的 body 前 500 字 (repr): "
            f"{body_text[:500]!r}"
        )

    # 5. dispatch
    if isinstance(action, Approve):
        await _handle_approve(db, pub_id, draft_id, action)
    elif isinstance(action, Reject):
        await _handle_reject(db, pub_id, draft_id, action)
    elif isinstance(action, Drop):
        await _handle_drop(db, pub_id, draft_id)
    elif isinstance(action, Unknown):
        # 不改 state, 回提示邮件让用户重发
        await _send_unknown_reply(
            pub_id=pub_id,
            raw=action.raw,
            original_subject=subject,
            to_addr=from_addr,
        )
    else:
        # 理论不可达 — Action sum type 已覆盖所有变体
        log.error(f"[m3] 未知 Action 类型 pub_id={pub_id}: {type(action)!r}")


__all__ = ["handle_approval_reply", "extract_pub_id"]
