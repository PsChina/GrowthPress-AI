"""m5 撤销回信 handler — m4 未做, 本文件是 stub.

PUB 通道语义 (memory project_growthpress_human_loop.md L182):
  PUB 邮件不需要解析关键词, 任意非空 reply = 触发撤销, 撤销后不可恢复.

W3+ m4 publisher 完成后, 这里要:
  1. 从 subject 提 token (PUB-<id>) → 查 publications 表
  2. 检查 retract_window_until 未过期
  3. INSERT retractions state='requested' → 触发 m4 撤销流程
"""
from __future__ import annotations

import logging

from ...db import Database
from ..schemas import InboundMsg

log = logging.getLogger("growthpress.mailbox.retract_reply")


async def handle_retract_reply(msg: InboundMsg, db: Database) -> None:
    """W3+ stub: m4 publisher 未做, 暂时只打日志.

    真实实现待 m4 完成后接 retractions 表 + Publisher 撤销 API.
    """
    log.warning(
        f"[m5] retract_reply stub (m4 W3+ 未实施): "
        f"subject={msg.subject!r} sender={msg.sender!r} uid={msg.uid}"
    )
