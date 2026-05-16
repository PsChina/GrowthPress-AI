"""m5 mailbox dataclass — 入站邮件结构 + 发布意图.

InboundMsg 是 dispatcher 与 handler 之间统一的数据契约:
  poller 拿到 raw IMAP 消息后, 解析成 InboundMsg, 再交给 dispatcher.
  这样 dispatcher / handler 不直接依赖 aioimaplib / imaplib 任一具体库.

PublishIntent 是 looks_like_complete_article 配套的发布意图解析结果:
  邮件正文里如果含 INTENT_KEYWORDS_DIRECT / REVIEW, 解析成结构化字段,
  传给 email_content handler 决定跳过哪些模块. (m2 合规仍强制不能 skip).
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class InboundMsg:
    """已解析的入站邮件 (从 poller 喂给 dispatcher / handler).

    字段:
      uid:        IMAP UID (整数), 用于标 \\Seen 防重复; 单测可填 0
      subject:    主题原文 (含 Re: / 前缀)
      sender:     From 头 (RFC822 格式, e.g. "Name <addr@x.com>")
      body_text:  纯文本正文 (multipart 选 text/plain, 没有再退到 strip HTML)
      message_id: Message-ID 头, 备查 (subject token 已够定位, MID 冗余更稳)
      in_reply_to: In-Reply-To 头, 关联原邮件 (subject token 已够, 这里冗余)
      received_at: 收到时间 (ISO 字符串)
    """
    uid: int
    subject: str
    sender: str
    body_text: str
    message_id: str = ""
    in_reply_to: str = ""
    received_at: str = ""


@dataclass(frozen=True)
class PublishIntent:
    """从邮件正文解析出的发布意图. 用于 email_content handler.

    skip_m2_quality: True 跳质量审 (但 m2 合规仍强制, 不能 skip)
    skip_m3:         True 跳前置人工确认 APV

    "直接发"语义 → 二者都 True; "审一下"语义 → 二者都 False;
    默认保守 → 二者都 False (走完整流程).
    """
    skip_m2_quality: bool = False
    skip_m3: bool = False


__all__ = ["InboundMsg", "PublishIntent"]
