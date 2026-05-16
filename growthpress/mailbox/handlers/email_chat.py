"""m5 邮件 → claude chat handler.

用户邮件 subject 含 "发"/"发一篇" 等发布关键字 → INSERT email_intake(intent='post')
state='pending'. 由独立 daemon task `email_chat_dispatcher` 异步消费 (起 claude
subprocess 跑 growthpress-post skill 全套). 这里只负责"收 + 入库", 不阻塞 IMAP poller.

为什么不复用 drafts.state='new_from_email_topic':
  - drafts 表语义是"最终要发布的内容", 把 chat 命令塞进去会污染 state machine
  - email_intake 独立追踪 sender/message_id/intent, claude 失败时单独标 failed,
    不需要绕道 drafts.state=archived
  - message_id UNIQUE 防 IMAP poller 重拉重跑 (drafts 没这层保护)
"""
from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime, timezone

from ...db import Database
from ..schemas import InboundMsg

log = logging.getLogger("growthpress.mailbox.email_chat")

# 触发"派任务给 claude"意图的关键字 (subject 或 body 前 300 字命中即判 post).
# 故意宽松 — 用户怎么写都行, 比死板要求"必须含 '发'" 友好得多.
# 三类:
#   1. 明确发布动作: 发 / 发一篇 / 发布 / 发个 / 来一篇 / 推一篇 / 帮我发
#   2. 派工措辞: 新任务 / 任务 / 帮我 / 请你 / 麻烦你 / 写一篇 / 写个 / 起一篇 / 来个
#   3. 显式 @ claude (英文 / 中文)
_POST_INTENT_KEYWORDS = (
    # 发布
    "发一篇", "发个", "发布", "推一篇", "帮我发", "来一篇",
    # 派工
    "新任务", "任务", "帮我", "请你", "麻烦你", "写一篇", "写个", "起一篇", "来个",
    # @ claude
    "@claude", "@Claude", "claude",
    # 通用动词 (单字) — 放最后, 配合优先级判定
    "发",
)


def classify_intent(subject: str, body_text: str) -> str | None:
    """识别邮件意图. MVP 命中即判 'post' (走发布工作流).

    subject + body 前 300 字一起扫. 任一命中关键字即返 'post'. 通用 chat
    (非派工类) 暂返 None, 走原有 email_topic 路径.
    """
    haystack = (subject or "") + "\n" + (body_text or "")[:300]
    for kw in _POST_INTENT_KEYWORDS:
        if kw in haystack:
            return "post"
    return None


async def handle_email_chat(msg: InboundMsg, db: Database, intent: str) -> str | None:
    """INSERT email_intake state='pending'. 返回 intake_id, 或 None 表示重复忽略.

    message_id UNIQUE 约束: 同邮件 IMAP poller 第二次拉到时 INSERT 会失败, 静默返 None.
    """
    intake_id = uuid.uuid4().hex[:8]
    now = datetime.now(timezone.utc).isoformat()

    # message_id 缺失时拼一个 fallback (理论不该发生, IMAP RFC 强制有, 但保护下)
    message_id = msg.message_id or f"no-msg-id-{intake_id}"

    try:
        await db.write(
            """INSERT INTO email_intake
               (id, message_id, sender, subject, body_text, intent, state,
                created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)""",
            (intake_id, message_id, msg.sender, msg.subject,
             msg.body_text or "", intent, now, now),
        )
    except Exception as e:
        # UNIQUE 冲突: 同 message_id 已入库, 这是 IMAP 重拉. 静默忽略.
        err = str(e)
        if "UNIQUE" in err or "unique" in err:
            log.info(
                f"[m5/chat] message_id={message_id!r} 已入库, 跳过 (IMAP 重拉)"
            )
            return None
        raise

    log.info(
        f"[m5/chat] email_intake 入库 intake_id={intake_id} intent={intent!r} "
        f"sender={msg.sender!r} subject={msg.subject[:60]!r}"
    )
    return intake_id


__all__ = ["handle_email_chat", "classify_intent"]
