"""m4 published 后给 notify_to 发通知邮件.

两种:
  - send_pub_notification         成功通知 [PUB-...] (任意回信触发撤销)
  - send_publish_failure_notification 失败通知 [FAIL-...] (人工排查)

按 memory project_growthpress_human_loop.md PUB 模板.
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
        from ..shared.smtp import smtp_send
        await smtp_send(msg)
        log.info(f"[pub_email] sent {subject!r} to {s.notify_to}")
        return True
    except Exception as e:
        log.error(
            f"[pub_email] SMTP send failed for PUB {publication_id}: {e!r}"
        )
        return False


async def send_publish_failure_notification(
    *,
    publication_id: str,
    draft_id: str,
    title: str,
    platform: str,
    error: str | None,
    error_detail: str | None,
    elapsed_sec: float,
    dry_run: bool = False,
) -> bool:
    """发 [FAIL-{publication_id}-{platform}] 失败通知邮件.

    设计:
      - 不可观测的 silent failure 是数字员工最危险的失败模式. 失败必须通知.
      - dry_run=True 时不通知 (本来就不会真发, 失败也是预期).
      - SMTP 未配 / 通知发送失败仅 log, 不抛 (邮件失败不应放大 publisher 失败).
    """
    if dry_run:
        log.info(f"[pub_email] dry_run, 跳过 FAIL {publication_id} {platform}")
        return False

    s = get_settings()
    if not s.smtp_user or not s.notify_to:
        log.warning(
            f"[pub_email] SMTP / notify_to 未配置, 跳过 FAIL {publication_id} {platform}"
        )
        return False

    subject = f"[FAIL-{publication_id}-{platform}] 发布失败: 《{title}》"
    body = (
        f"❌ 发布到 {platform} 失败\n"
        f"\n"
        f"错误码    : {error or '(未知)'}\n"
        f"错误细节  : {error_detail or '(无)'}\n"
        f"耗时      : {elapsed_sec:.1f}s\n"
        f"\n"
        f"draft 状态已转 human_queue, 不再重试. 排查方式:\n"
        f"  本地查 draft 详情: python -m growthpress draft show {draft_id}\n"
        f"  本地看 daemon 日志: tail -100 ~/Library/Logs/growthpress/*.log (或 launchd stdout)\n"
        f"  常见原因:\n"
        f"    - session 失效  → 重登 (xhs: xhs-publisher login)\n"
        f"    - 平台改版      → 选择器 / API 更新需要 publisher 升级\n"
        f"    - 内容审核拒    → 标题/正文/图触发平台风控\n"
        f"    - 网络超时      → publisher single_platform_timeout (默认 300s)\n"
        f"\n"
        f"修好后, 把 draft 拉回 approved 重发: \n"
        f"  sqlite3 data/runs.db \"UPDATE drafts SET state='approved' WHERE id='{draft_id}'\"\n"
        f"  daemon m3_pump 60s 内会再发一封 APV.\n"
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
        from ..shared.smtp import smtp_send
        await smtp_send(msg)
        log.info(f"[pub_email] sent FAIL notification {subject!r}")
        return True
    except Exception as e:
        log.error(
            f"[pub_email] SMTP send failed for FAIL {publication_id}: {e!r}"
        )
        return False
