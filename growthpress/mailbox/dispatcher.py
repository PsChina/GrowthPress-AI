"""m5 dispatcher — 把已解析的 InboundMsg 路由到对应 handler.

6 通道 (memory project_growthpress_human_loop.md L16-L23):
  APV / PUB / REJECT / email_topic / email_content / ROUTE-*

REJECT 是出邮件方向 — 如果用户回了 REJECT (不该回), 当 UNKNOWN 走启发式
(大概率会被判成"完整文章"或"喂选题"). 这里实际处理: log warning + 仍走启发式,
不静默丢 (用户写了东西, 总要响应).

m3 import 容错: from growthpress.approver import handle_approval_reply 若失败
(并行 agent 未完成时), 用 importlib 延迟 import + log warning.
"""
from __future__ import annotations

import importlib
import logging
from typing import Any, Awaitable, Callable

from ..db import Database
from .handlers import (
    classify_intent,
    handle_email_chat,
    handle_email_content,
    handle_email_topic,
    handle_retract_reply,
    handle_route_command,
)
from .heuristic import looks_like_complete_article
from .schemas import InboundMsg
from .subject_parser import SubjectKind, parse_subject

log = logging.getLogger("growthpress.mailbox.dispatcher")

# m3 handler import 字符串. 并行 agent 完成后这里可直接 from growthpress.approver
# import handle_approval_reply, 当前用 importlib 延迟以容错.
_M3_HANDLER = "growthpress.approver:handle_approval_reply"


async def _call_approval_reply(msg: InboundMsg, db: Database) -> None:
    """延迟 import m3 handler. 若 m3 尚未实现 (并行 agent 未完成), log + 跳过.

    m3.handle_approval_reply 入参约定: dict {subject, body_text, message_id, from_addr}
    (m3 实现文件 approver/handler.py L160-L181). 这里负责把 InboundMsg 适配成 dict —
    我们的 InboundMsg.sender 是 RFC822 ("Name <addr>"), m3 期望的 from_addr 是纯
    邮箱串, 用 email.utils.parseaddr 抽出.
    """
    from email.utils import parseaddr
    _, from_addr = parseaddr(msg.sender or "")

    payload = {
        "subject": msg.subject,
        "body_text": msg.body_text,
        "message_id": msg.message_id,
        "from_addr": from_addr or msg.sender,
    }

    mod_path, attr = _M3_HANDLER.split(":", 1)
    try:
        mod = importlib.import_module(mod_path)
    except ImportError as e:
        log.warning(
            f"[m5/dispatch] m3 module {mod_path!r} 不可 import (并行 agent 未完成?): "
            f"{e!r}. APV 回信暂存 log, 不丢: subject={msg.subject!r}"
        )
        return
    fn = getattr(mod, attr, None)
    if fn is None:
        log.warning(
            f"[m5/dispatch] m3 {mod_path}:{attr} 不存在. "
            f"APV 回信暂存 log: subject={msg.subject!r}"
        )
        return
    await fn(payload, db)


async def dispatch_inbound(msg: InboundMsg, db: Database) -> None:
    """单封邮件按 subject + 正文启发式分发到 6 通道之一. 异常向上抛 (poller 兜).

    自反馈防护: SMTP_USER == IMAP_USER (同一邮箱发收) 时, daemon 自己发的 PUB / FAIL /
    Unknown 提示信 / 日报 等会被 IMAP 拉回. 它们带 [PUB-] [FAIL-] 等 daemon 自身 prefix,
    不属于"用户命令". 直接丢弃避免被 email_topic 当成新 draft 入库.
    """
    info = parse_subject(msg.subject)
    log.info(
        f"[m5/dispatch] uid={msg.uid} kind={info.kind.value} "
        f"token={info.token!r} sender={msg.sender!r} "
        f"subject={msg.subject[:80]!r}"
    )

    # 防自反馈: subject 带 daemon 自身 prefix 的视为"自己发的", 直接丢弃.
    # 用户回 APV 时 subject 是 "Re: [APV-...]" — APPROVAL_REPLY 在下面单独路由, 不走这里.
    # daemon 自己发的 [PUB-...] [FAIL-...] [PEND-...] 主题前缀都进这个黑名单.
    self_prefixes = ("[pub-", "[fail-", "[pend-")
    subject_lower = (msg.subject or "").lower().lstrip()
    # 兼容 "回复: [FAIL-...]" / "Re: [FAIL-...]" — 用户不应该回 daemon 的通知信,
    # 即使回了, 也按本身 prefix 决定走的通道 (RETRACT_REPLY 等), 别进 email_topic.
    if any(p in subject_lower for p in self_prefixes):
        if info.kind == SubjectKind.UNKNOWN:
            log.info(
                f"[m5/dispatch] 丢弃 daemon 自身通知邮件 (无路由): "
                f"subject={msg.subject[:80]!r}"
            )
            return

    if info.kind == SubjectKind.APPROVAL_REPLY:
        await _call_approval_reply(msg, db)
        return

    if info.kind == SubjectKind.RETRACT_REPLY:
        await handle_retract_reply(msg, db)
        return

    if info.kind == SubjectKind.REJECT_NOTICE:
        # REJECT 不该有回信. 用户回了 REJECT → 当作"重新提交"走启发式.
        log.info(
            f"[m5/dispatch] REJECT 邮件被回信 (用户应发新邮件而非回复), "
            f"降级走启发式 dispatch: subject={msg.subject!r}"
        )
        # fall through to 启发式分支 (无 token 路径)

    elif info.kind in (
        SubjectKind.ROUTE_ADD,
        SubjectKind.ROUTE_REMOVE,
        SubjectKind.ROUTE_LIST,
        SubjectKind.ROUTE_DEFAULT,
        SubjectKind.ROUTE_CRON,
    ):
        await handle_route_command(msg, db)
        return

    # UNKNOWN (或降级的 REJECT) — 优先识别 chat 意图 (subject/body 关键字)
    # 命中"发"/"发一篇" 等关键字 → email_intake 队列 → email_chat_dispatcher 起 claude.
    # 不命中走原有启发式 (完整文章 / 喂选题). 选题路径会在 chat 闭环成熟后再统一收编.
    chat_intent = classify_intent(msg.subject, msg.body_text)
    if chat_intent:
        await handle_email_chat(msg, db, intent=chat_intent)
        return

    if looks_like_complete_article(msg.body_text):
        await handle_email_content(msg, db)
    else:
        await handle_email_topic(msg, db)


__all__ = ["dispatch_inbound"]
