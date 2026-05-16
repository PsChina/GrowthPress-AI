"""m5 用户喂完整文章 handler — 入 drafts state='new_from_email_content'.

dispatcher 判定: 无 token + looks_like_complete_article=True → 这里.

行为:
  1. 用 heuristic.parse_publish_intent() 提取 skip_m2_quality / skip_m3
  2. INSERT drafts (state='new_from_email_content', body_md=正文,
     topic="user-submitted", title=subject 首行)
  3. 落 state_log + 在 reason 记录 intent (调试 / 后续读)

发布意图 (skip_m2_quality / skip_m3) 不在 drafts schema 里 — 当前 schema 没有
intent 字段. V1 简化: 把 intent 写进 state_log.reason 让后续模块读取
("intent: skip_m2_quality=True skip_m3=True"). 后续如果扩展 drafts schema,
加专门字段更稳; V1 用 reason 作为信使是可接受的妥协.

m2 合规仍强制 — handler 不解析合规跳过指令, m2 自己拦着. 这里只记 intent.
"""
from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime, timezone

from ...db import Database
from ..heuristic import parse_publish_intent
from ..schemas import InboundMsg

log = logging.getLogger("growthpress.mailbox.email_content")

_SUBJECT_PREFIX_RE = re.compile(
    r"^(?:re|fwd|fw|回复|转发|答复)\s*:\s*",
    re.IGNORECASE,
)


def _extract_title(subject: str, body: str) -> str:
    """title 优先用 subject (去前缀), 若空再退到 body 第一个 markdown header 或首行."""
    s = (subject or "").strip()
    while True:
        new = _SUBJECT_PREFIX_RE.sub("", s, count=1).strip()
        if new == s:
            break
        s = new
    if len(s) >= 3:
        return s[:200]

    # fallback 1: 找 body 第一个 # / ## / ### header
    m = re.search(r"^#{1,3}\s+(.+)$", body or "", re.MULTILINE)
    if m:
        return m.group(1).strip()[:200]

    # fallback 2: body 首行
    for line in (body or "").splitlines():
        line = line.strip()
        if line:
            return line[:200]
    return "(用户提交的文章 — 无标题)"


async def handle_email_content(msg: InboundMsg, db: Database) -> str:
    """INSERT drafts state='new_from_email_content'. 返回新 draft_id."""
    intent = parse_publish_intent(msg.body_text)
    title = _extract_title(msg.subject, msg.body_text)
    body_md = msg.body_text or ""
    draft_id = uuid.uuid4().hex[:8]
    now = datetime.now(timezone.utc).isoformat()

    await db.write(
        """INSERT INTO drafts
           (id, topic, title, body_md, summary, sources, state, created_at, updated_at)
           VALUES (?, ?, ?, ?, NULL, NULL, 'new_from_email_content', ?, ?)""",
        (draft_id, "user-submitted", title, body_md, now, now),
    )
    # intent 写进 state_log.reason 供下游模块读 (drafts schema 没专门字段)
    reason = (
        f"email content from {msg.sender!r}, title={title[:80]!r}, "
        f"intent: skip_m2_quality={intent.skip_m2_quality} skip_m3={intent.skip_m3}"
    )
    await db.log_initial_state(
        draft_id,
        to_state="new_from_email_content",
        by_agent="m5",
        reason=reason,
    )
    log.info(
        f"[m5] email_content 入库: draft_id={draft_id} title={title[:80]!r} "
        f"intent=(skip_m2_quality={intent.skip_m2_quality}, skip_m3={intent.skip_m3}) "
        f"body_len={len(body_md)}"
    )
    return draft_id
