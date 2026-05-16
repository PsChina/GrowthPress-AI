"""m5 mailbox handlers — 按通道分文件 (单一职责).

handlers 都是 async, 参数固定 (msg: InboundMsg, db: Database). dispatcher 调用,
失败抛异常 (poller 在外层 try/except 包住, 不挂 task).
"""
from .email_chat import classify_intent, handle_email_chat
from .email_content import handle_email_content
from .email_topic import handle_email_topic
from .retract_reply import handle_retract_reply
from .route_command import handle_route_command

__all__ = [
    "classify_intent",
    "handle_email_chat",
    "handle_email_topic",
    "handle_email_content",
    "handle_retract_reply",
    "handle_route_command",
]
