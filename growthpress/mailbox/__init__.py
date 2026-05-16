"""m5 mailbox — IMAP poller + 6 通道入站邮件分发.

公开 API:
    imap_poller_run(db)              — 长生命周期 task, orchestrator 起
    dispatch_inbound(msg, db)        — 单封邮件分发 (供测试 / CLI 注入用)
    looks_like_complete_article(body)— 启发式判别 (供测试 / 单点调试)
    parse_publish_intent(body)       — 发布意图解析
    InboundMsg                       — 入站邮件 dataclass

6 通道 (memory project_growthpress_human_loop.md L16-L23):
    [APV-<id>]    → growthpress.approver.handle_approval_reply  (m3, 容错 import)
    [PUB-<id>]    → handlers.handle_retract_reply               (m4 W3+ stub)
    无 token+短   → handlers.handle_email_topic                 (drafts state=new_from_email_topic)
    无 token+长   → handlers.handle_email_content               (drafts state=new_from_email_content)
    [ROUTE-*]     → handlers.handle_route_command               (改 config/topics.yaml)
    [REJECT-<id>] → 出邮件方向, 收到回信 → log + 降级走启发式
"""
from .dispatcher import dispatch_inbound
from .heuristic import (
    INTENT_KEYWORDS_DIRECT,
    INTENT_KEYWORDS_REVIEW,
    looks_like_complete_article,
    parse_publish_intent,
)
from .poller import POLL_INTERVAL_SEC, imap_poller_run
from .schemas import InboundMsg, PublishIntent
from .subject_parser import SubjectInfo, SubjectKind, parse_subject

__all__ = [
    "imap_poller_run",
    "dispatch_inbound",
    "looks_like_complete_article",
    "parse_publish_intent",
    "InboundMsg",
    "PublishIntent",
    "SubjectKind",
    "SubjectInfo",
    "parse_subject",
    "POLL_INTERVAL_SEC",
    "INTENT_KEYWORDS_DIRECT",
    "INTENT_KEYWORDS_REVIEW",
]
