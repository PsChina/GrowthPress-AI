"""m4 published 后给 notify_to 发 [PUB-{publication_id}-{platform}] 通知邮件.

按 memory project_growthpress_human_loop.md PUB 模板.
任意回信 = 触发撤销 (m5 retract_reply 处理, W3 实装).
"""
from __future__ import annotations

import logging
from email.message import EmailMessage

import aiosmtplib

from ..core.settings import get_settings

log = logging.getLogger("growthpress.publisher.pub_email")


async def send_pub_notification(
    *,
    publication_id: str,
    draft_id: str,
    title: str,
    platform: str,
    url: str | None,
    published_at: str,
    retract_until: str,
    dry_run: bool = False,
) -> bool:
    """发 [PUB-{publication_id}-{platform}] 通知邮件.

    dry_run=True 时 log 不发. SMTP 未配置 / 失败仅 log, 不抛 (PUB 不影响发布成功).
    返回 True 表示真发送了, False 表示未发 (dry_run / 未配 / 错误).
    """
    if dry_run:
        log.info(f"[pub_email] dry_run, 跳过 PUB {publication_id} {platform}")
        return False

    s = get_settings()
    if not s.smtp_user or not s.notify_to:
        log.warning(
            f"[pub_email] SMTP / notify_to 未配置, 跳过 PUB {publication_id} {platform}"
        )
        return False

    subject = f"[PUB-{publication_id}-{platform}] 已发布: 《{title}》"
    body = (
        f"✅ 已发布到 {platform}\n"
        f"URL: {url or '(等平台返回)'}\n"
        f"发布时间: {published_at}\n"
        f"撤销窗口: 24h ({retract_until} 前回信可撤销)\n"
        f"\n"
        f"要撤销? 回复任意内容均触发撤销, 撤销后不可恢复.\n"
        f"\n"
        f"—\n"
        f"GrowthPress AI 自动通知. draft_id={draft_id}\n"
    )

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = s.smtp_user
    msg["To"] = s.notify_to
    msg.set_content(body)

    try:
        await aiosmtplib.send(
            msg,
            hostname=s.smtp_host,
            port=s.smtp_port,
            username=s.smtp_user,
            password=s.smtp_pass.get_secret_value(),
            start_tls=True,
        )
        log.info(f"[pub_email] sent {subject!r} to {s.notify_to}")
        return True
    except Exception as e:
        log.error(
            f"[pub_email] SMTP send failed for PUB {publication_id}: {e!r}"
        )
        return False
