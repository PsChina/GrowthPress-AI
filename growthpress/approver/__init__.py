"""m3 approver — 前置人工确认 (邮件 APV).

公开 API:
    send_approval(draft_id, db) -> pub_id | None        # 发 APV 邮件 (approved → pending_human)
    handle_approval_reply(message, db) -> None          # 处理回信 (m5 dispatcher 调用)
    parse_reply(body) -> Action                         # 解析回信首词 (pure function)

State 变迁 (drafts 表):
    approved        → pending_human    (send_approval)
    pending_human   → publishing       (Approve)
    pending_human   → revising         (Reject, revise_count++)
    pending_human   → archived         (Drop)

详见 [[project-growthpress-human-loop]].
"""
from .agent import APV_TIMEOUT_HOURS, send_approval
from .handler import extract_pub_id, handle_approval_reply
from .reply_parser import KNOWN_PLATFORMS, parse_reply
from .schemas import Action, Approve, Drop, Reject, Unknown

__all__ = [
    # 主入口
    "send_approval",
    "handle_approval_reply",
    "parse_reply",
    # 辅助
    "extract_pub_id",
    "APV_TIMEOUT_HOURS",
    "KNOWN_PLATFORMS",
    # Action types
    "Action",
    "Approve",
    "Reject",
    "Drop",
    "Unknown",
]
