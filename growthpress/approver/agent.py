"""m3 approver agent: 发 APV 邮件 (前置人工确认).

流程:
  1. CAS 锁 state: approved → pending_human
  2. 生成 pub_id (uuid 前 6 位)
  3. 拿 draft 详情 (title / summary / body_md + file:// 路径)
  4. INSERT approvals (state=pending, expires_at=now+24h)
  5. SMTP 发邮件给 settings.notify_to (含图片附件 inline)
  6. 返回 pub_id; 任一步失败回滚 state 到 approved

设计要点:
  - state 锁优先 — 没拿到锁直接返 None, 不浪费后续 IO
  - SMTP 失败要回滚 state (pending_human → approved) 让 orchestrator 下轮重试,
    不能让 draft 卡在 pending_human 但 approvals 没记录 / 邮件没发
  - approvals INSERT 在 SMTP 之前 — 万一 SMTP 失败可以靠 approvals 行追溯;
    回滚时同时清掉 approvals 行避免下一轮 pub_id 冲突
"""
from __future__ import annotations

import json
import logging
import mimetypes
import os
import uuid
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path

import aiosmtplib

from ..core.settings import get_settings
from ..db import Database
from .email_template import (
    DEFAULT_PLATFORMS,
    build_apv_body,
    build_apv_subject,
)

log = logging.getLogger("growthpress.approver")

# APV 失效窗口 — memory human_loop.md L142 "24h 后挂起". 后续可移到 settings.
APV_TIMEOUT_HOURS = 24

# 草稿 markdown 落盘目录 — 与 scout_writer 对齐 (data/drafts/<id>.md).
# 后续若 settings 加 drafts_dir 字段, 改这里读 settings.
DRAFTS_DIR = Path("data/drafts")

# APV 邮件图片附件单张大小上限 (bytes). 超过跳过并 warn, 不中断发送.
_MAX_IMG_BYTES = 5 * 1024 * 1024  # 5 MB


def _attach_images(msg: EmailMessage, media_json: str | None) -> None:
    """从 draft.media (JSON list of local paths) 读图, 以 inline 附件加入 msg.

    策略:
      - 解析失败 / 列表为空 → 静默跳过
      - 单张文件不存在 / 读失败 / 超 5 MB → log warning, 跳过该张, 其余继续
      - Content-Disposition: inline 让 163 / Gmail 等客户端直接展开显示
      - Content-ID: <img-N> 供 body 里 [cid:img-N] 引用 (plain text 客户端看不到, 附件仍展开)
    """
    try:
        paths = json.loads(media_json or "[]")
    except Exception as e:
        log.warning(f"[m3] media JSON 解析失败 ({e!r}), 跳过图片附件")
        return

    if not isinstance(paths, list) or not paths:
        return

    attached = 0
    for i, p_str in enumerate(paths):
        path = Path(p_str)
        if not path.is_file():
            log.warning(f"[m3] media 文件不存在, 跳过: {path}")
            continue
        try:
            size = os.path.getsize(path)
        except OSError as e:
            log.warning(f"[m3] media stat 失败, 跳过 {path}: {e!r}")
            continue
        if size > _MAX_IMG_BYTES:
            log.warning(
                f"[m3] media 文件过大 ({size/1024/1024:.1f} MB > 5 MB), 跳过: {path}"
            )
            continue
        try:
            img_data = path.read_bytes()
        except OSError as e:
            log.warning(f"[m3] media 读取失败, 跳过 {path}: {e!r}")
            continue

        ctype, _ = mimetypes.guess_type(path.name)
        if not ctype or not ctype.startswith("image/"):
            ctype = "image/jpeg"
        maintype, subtype = ctype.split("/", 1)
        cid = f"img-{i}"

        # add_related: Content-Disposition inline, 带 Content-ID
        # 163 / Gmail 客户端会在邮件正文区展开显示, 不需要点"附件"
        msg.add_related(
            img_data,
            maintype=maintype,
            subtype=subtype,
            filename=path.name,
            cid=f"<{cid}>",
        )
        attached += 1
        log.debug(f"[m3] 附图 {path.name} ({size/1024:.0f} KB) cid={cid}")

    if attached:
        log.info(f"[m3] APV 邮件附图 {attached} 张")


async def _send_smtp(*, subject: str, body: str, to_addr: str, media_json: str | None = None) -> None:
    """SMTP 发送. settings 取 host/port/user/pass, STARTTLS (Gmail 587).

    media_json: draft.media 字段原始 JSON 字符串 (可选). 有图则以 inline 附件加入.
    失败抛 aiosmtplib.SMTPException, 由 caller 决定回滚 / 重试.
    """
    s = get_settings()
    msg = EmailMessage()
    msg["From"] = s.smtp_user
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg.set_content(body, charset="utf-8")

    # 附图 (容错: 失败只 log, 不阻断邮件发送)
    try:
        _attach_images(msg, media_json)
    except Exception as e:
        log.warning(f"[m3] _attach_images 异常 (邮件仍发出): {e!r}")

    from ..shared.smtp import smtp_send
    await smtp_send(msg)


async def _rollback_state(
    db: Database, draft_id: str, pub_id: str | None, reason: str
) -> None:
    """回滚: pending_human → approved + 清掉 approvals row (如已 INSERT).

    不抛异常 — 回滚本身失败只 log warning, 不让回滚错误掩盖原始错误.
    """
    try:
        if pub_id:
            await db.write("DELETE FROM approvals WHERE id=?", (pub_id,))
        ok = await db.transition(
            draft_id, "pending_human", "approved", "m3",
            reason=f"rollback: {reason}",
        )
        if not ok:
            log.warning(
                f"[m3] rollback state 失败 (draft={draft_id} 不在 pending_human, "
                "可能被并发改过), 需人工排查"
            )
    except Exception as e:
        log.warning(f"[m3] rollback 自身失败 draft={draft_id}: {e!r}")


async def send_approval(draft_id: str, db: Database) -> str | None:
    """发 APV 邮件主入口.

    Args:
        draft_id: drafts.id, 必须处于 state='approved'.
        db: Database 实例.

    Returns:
        pub_id (uuid 前 6 位) 表示成功; None 表示锁状态失败 / draft 不存在 / SMTP 失败回滚.
    """
    s = get_settings()
    if not s.notify_to:
        log.error("[m3] settings.notify_to 未配置, 跳过 APV")
        return None
    if not s.smtp_user:
        log.error("[m3] settings.smtp_user 未配置, 跳过 APV")
        return None

    # 1. 拿 draft 详情 (state CAS 之前先读, 避免锁住后才发现 draft 数据缺失)
    rows = await db.read("SELECT * FROM drafts WHERE id=?", (draft_id,))
    if not rows:
        log.error(f"[m3] draft {draft_id!r} 不存在")
        return None
    draft = dict(rows[0])

    # 2. CAS 锁 state: approved → pending_human
    locked = await db.transition(
        draft_id, "approved", "pending_human", "m3",
        reason="APV sent",
    )
    if not locked:
        log.warning(
            f"[m3] {draft_id!r} 不在 approved state (当前={draft.get('state')!r}), 跳过"
        )
        return None

    # 3. 生成 pub_id (uuid 前 6 位, 与 human_loop.md L106 "7f3a2b" 一致)
    pub_id = uuid.uuid4().hex[:6]
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(hours=APV_TIMEOUT_HOURS)

    # 4. INSERT approvals 行 (pending, reminder_count=0).
    #    放在 SMTP 之前 — 失败回滚要 DELETE; 成功后即使 SMTP 重发也有追溯.
    try:
        await db.write(
            "INSERT INTO approvals "
            "(id, draft_id, sent_at, expires_at, state, reminder_count) "
            "VALUES (?, ?, ?, ?, 'pending', 0)",
            (pub_id, draft_id, now.isoformat(), expires_at.isoformat()),
        )
    except Exception as e:
        log.error(f"[m3] INSERT approvals 失败 draft={draft_id}: {e!r}")
        await _rollback_state(db, draft_id, None, f"approvals insert fail: {e}")
        return None

    # 5. 构造邮件 + 发送
    title = draft.get("title") or "(无标题)"
    summary = draft.get("summary") or ""
    body_md = draft.get("body_md") or ""
    media_json = draft.get("media")  # JSON list of local image paths (may be None / '[]')
    draft_path = DRAFTS_DIR / f"{draft_id}.md"

    subject = build_apv_subject(pub_id, title)
    body = build_apv_body(
        pub_id=pub_id,
        title=title,
        summary=summary,
        body_md=body_md,
        draft_path=draft_path,
        expires_at=expires_at,
        platforms=DEFAULT_PLATFORMS,
    )

    log.info(
        f"[m3] send APV draft={draft_id} pub_id={pub_id} to={s.notify_to} "
        f"title={title!r}"
    )
    try:
        await _send_smtp(subject=subject, body=body, to_addr=s.notify_to, media_json=media_json)
    except Exception as e:
        log.error(f"[m3] SMTP 发送失败 pub_id={pub_id}: {e!r}")
        await _rollback_state(db, draft_id, pub_id, f"smtp fail: {e}")
        return None

    log.info(f"[m3] APV 发送成功 draft={draft_id} pub_id={pub_id} expires_at={expires_at.isoformat()}")
    return pub_id


__all__ = ["send_approval", "APV_TIMEOUT_HOURS"]
