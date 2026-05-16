"""统一 SMTP 发送 helper. 自动判断 SSL (465) vs STARTTLS (587).

为什么需要:
  aiosmtplib 区分 `use_tls=True` (从一开始就 SSL, 用 465) vs `start_tls=True`
  (普通连接后 STARTTLS 升级, 用 587). 用错了会 ConnectionRefusedError / 握手失败.

  国内邮箱 (163 / QQ / 126 / 新浪) 主推 465 SSL,
  国外邮箱 (Gmail / Outlook) 主推 587 STARTTLS.
  代码原来硬编码 start_tls=True 只兼容国外, 国内邮箱发不出. 这里按 port 自动判断.

  非 465 / 587 端口 (理论上 25 明文) 走 STARTTLS 兜底, 失败抛 SMTPException.
"""
from __future__ import annotations

import logging
from email.message import EmailMessage

import aiosmtplib

from ..core import get_settings

log = logging.getLogger("growthpress.shared.smtp")


async def smtp_send(email: EmailMessage, *, timeout: float = 30) -> None:
    """统一发送入口. 按 settings.smtp_port 自动选 SSL / STARTTLS.

    Args:
        email: 已填好 From / To / Subject / Body 的 EmailMessage.
        timeout: 连接 + 发送总超时. 默认 30s.

    Raises:
        aiosmtplib.SMTPException 等. 调用方决定 try / 不 try.
    """
    s = get_settings()

    if s.smtp_port == 465:
        # 国内邮箱主流: 一开始就 SSL
        use_tls, start_tls = True, False
    elif s.smtp_port == 587:
        # Gmail / Outlook 主流: 普通连接后 STARTTLS
        use_tls, start_tls = False, True
    elif s.smtp_port == 25:
        # 明文端口 (大部分公网都封了), 兜底也尝试 STARTTLS
        use_tls, start_tls = False, True
    else:
        # 未知端口, 默认 STARTTLS (跟原行为一致)
        log.warning(
            f"[smtp] 非常规端口 {s.smtp_port}, 默认 STARTTLS — 不通就改 .env 端口"
        )
        use_tls, start_tls = False, True

    await aiosmtplib.send(
        email,
        hostname=s.smtp_host,
        port=s.smtp_port,
        username=s.smtp_user,
        password=s.smtp_pass.get_secret_value(),
        use_tls=use_tls,
        start_tls=start_tls,
        timeout=timeout,
    )
