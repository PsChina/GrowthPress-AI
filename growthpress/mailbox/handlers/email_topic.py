"""m5 用户喂选题 handler — 入 drafts 表 state='new_from_email_topic'.

dispatcher 判定: 无 token + 启发式 looks_like_complete_article=False → 这里.

行为:
  1. 从 subject + body 提取 topic (subject 优先, 去掉 Re:/Fwd: 前缀; subject 空就用 body 首行)
  2. INSERT drafts (state='new_from_email_topic', topic=<提取出>)
  3. 落 state_log

后续 (m1 / scheduler 后续接管):
  state='new_from_email_topic' 的 row 需要 m1 重新生成正文 (本 handler 不直接调 m1
  以避免 IMAP poller 阻塞 — 喂题入库后, 由独立的 scheduler / 或额外的 watcher 拉去跑).

  W3 简化: 本 handler 只入库, 不主动触发 m1. scout_writer.run() 可被未来一个独立
  email_topic_worker 异步消费. 这里保持 SRP — handler 只负责"收 + 入库".
"""
from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime, timezone

from ...db import Database
from ..schemas import InboundMsg

log = logging.getLogger("growthpress.mailbox.email_topic")

# 去 "Re: " "Fwd: " "回复:" "转发:" 等常见前缀
_SUBJECT_PREFIX_RE = re.compile(
    r"^(?:re|fwd|fw|回复|转发|答复)\s*:\s*",
    re.IGNORECASE,
)


def _extract_topic(subject: str, body: str) -> str:
    """subject 优先 (去前缀), 空 / 太短就用 body 首行非空."""
    s = (subject or "").strip()
    # 反复去前缀 (Gmail 转发链可能多层 Re: Re: Fwd:)
    while True:
        new = _SUBJECT_PREFIX_RE.sub("", s, count=1).strip()
        if new == s:
            break
        s = new
    if len(s) >= 3:
        return s[:200]

    # fallback: body 首行 (非空 strip)
    for line in (body or "").splitlines():
        line = line.strip()
        if line:
            return line[:200]
    return "(未指定 topic — 邮件 subject / body 均为空)"


async def handle_email_topic(msg: InboundMsg, db: Database) -> str:
    """INSERT drafts state='new_from_email_topic'. 返回新 draft_id.

    不直接调 m1 (避免阻塞 IMAP poller, 也避免与 scheduler 抢 LLM 配额).
    state='new_from_email_topic' 的 drafts 由后续独立 worker / scheduler 消费.
    """
    topic = _extract_topic(msg.subject, msg.body_text)
    draft_id = uuid.uuid4().hex[:8]
    now = datetime.now(timezone.utc).isoformat()

    await db.write(
        """INSERT INTO drafts
           (id, topic, title, body_md, summary, sources, state, created_at, updated_at)
           VALUES (?, ?, NULL, NULL, NULL, NULL, 'new_from_email_topic', ?, ?)""",
        (draft_id, topic, now, now),
    )
    await db.log_initial_state(
        draft_id,
        to_state="new_from_email_topic",
        by_agent="m5",
        reason=f"email topic from {msg.sender!r}: {topic[:80]}",
    )
    log.info(
        f"[m5] email_topic 入库: draft_id={draft_id} topic={topic[:80]!r} "
        f"sender={msg.sender!r}"
    )
    return draft_id
