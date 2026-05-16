"""m5 IMAP poller — 长生命周期 task, 每 30s 扫 INBOX 未读, 分发.

设计:
  - while True 循环, 每轮 fetch UNSEEN → parse → dispatch_inbound → STORE \\Seen
  - 单轮异常 try/except 包住, log + sleep 等下轮; 永不挂 task
  - 每轮新建 IMAP 连接 (不维持长连接 — 30s 间隔够稀疏, IDLE 模式开 socket
    成本不值; 失败重连也最简单)
  - smtp_user / imap_user 任一未配置 → task log 后直接退出 (开发期友好,
    不刷垃圾日志)

aioimaplib 主用; imaplib 兜底未实施 (aioimaplib 在 Python 3.13 / Gmail 实测 OK).

参考: memory project_growthpress_human_loop.md L215-L219
  - Gmail App Password
  - Message-ID / In-Reply-To 辅助关联
  - 处理过的标 \\Seen 防重复
  - 失败 retry (tenacity)
"""
from __future__ import annotations

import asyncio
import email
import email.policy
import logging
from email.message import EmailMessage
from email.utils import parseaddr
from typing import Any

from ..core import get_settings
from ..db import Database
from .dispatcher import dispatch_inbound
from .schemas import InboundMsg

log = logging.getLogger("growthpress.mailbox.poller")

POLL_INTERVAL_SEC = 30


# ---------- 邮件解析 (RFC822 → InboundMsg) ----------

def _decode_header(raw: str | None) -> str:
    """email.header.decode_header 后拼接成 str. None → ''."""
    if not raw:
        return ""
    from email.header import decode_header, make_header
    try:
        return str(make_header(decode_header(raw)))
    except Exception:
        return raw


def _extract_body_text(parsed: EmailMessage) -> str:
    """从 multipart 邮件提 text/plain. 没有再退到 strip HTML."""
    # 走 EmailMessage iter_parts / get_body. policy=default 已经把 charset 解码好了
    body = parsed.get_body(preferencelist=("plain",))
    if body is not None:
        try:
            return body.get_content().strip()
        except Exception as e:
            log.debug(f"[poller] text/plain get_content 失败: {e!r}")

    # 退 HTML — 简单 strip 标签 (不引第三方依赖)
    html_part = parsed.get_body(preferencelist=("html",))
    if html_part is not None:
        try:
            html = html_part.get_content()
            import re
            text = re.sub(r"<[^>]+>", " ", html)
            text = re.sub(r"\s+", " ", text).strip()
            return text
        except Exception as e:
            log.debug(f"[poller] text/html strip 失败: {e!r}")

    return ""


def _parse_rfc822(raw: bytes, uid: int) -> InboundMsg | None:
    """raw RFC822 bytes → InboundMsg. parse 失败返回 None."""
    try:
        parsed = email.message_from_bytes(raw, policy=email.policy.default)
        # type: EmailMessage
    except Exception as e:
        log.error(f"[poller] uid={uid} email parse 失败: {e!r}")
        return None

    subject = _decode_header(parsed.get("Subject"))
    sender_raw = _decode_header(parsed.get("From"))
    # parseaddr 返回 (name, addr), 我们保留原 RFC822 格式 (handler 回信用)
    name, addr = parseaddr(sender_raw)
    sender = f"{name} <{addr}>" if name and addr else (sender_raw or addr or "")
    message_id = (parsed.get("Message-ID") or "").strip()
    in_reply_to = (parsed.get("In-Reply-To") or "").strip()
    received_at = (parsed.get("Date") or "").strip()
    body = _extract_body_text(parsed)

    return InboundMsg(
        uid=uid,
        subject=subject,
        sender=sender,
        body_text=body,
        message_id=message_id,
        in_reply_to=in_reply_to,
        received_at=received_at,
    )


# ---------- IMAP 单轮 ----------

async def _poll_once(db: Database) -> int:
    """单轮: 连接 → search UNSEEN → fetch → dispatch → STORE \\Seen → 断开.

    返回本轮处理的邮件数 (含失败), 用于日志统计.
    任何异常向上抛 (poller 主循环兜).
    """
    from aioimaplib import aioimaplib  # 局部 import, 避免模块加载就强依赖

    s = get_settings()
    log.debug(f"[poller] 连接 {s.imap_host}:{s.imap_port} as {s.imap_user!r}")

    client = aioimaplib.IMAP4_SSL(host=s.imap_host, port=s.imap_port, timeout=30)
    await client.wait_hello_from_server()

    try:
        resp = await client.login(s.imap_user, s.imap_pass.get_secret_value())
        if resp.result != "OK":
            raise RuntimeError(f"IMAP login failed: {resp.result} {resp.lines}")

        # 网易 (163 / 126 / yeah.net) 强制要求客户端在 LOGIN 后发 IMAP ID 注明身份,
        # 否则 SELECT 报 "Unsafe Login. Please contact kefu@188.com". RFC 2971.
        #
        # aioimaplib.id(**kwargs) 默认序列化是 `( "name" "x" ... )` (括号后有空格),
        # 163 解析挂报 'BAD: Parse command error'. 必须手动构造 `("name" "x" ...)`
        # (左括号紧贴第一个字段). 用 raw Command 绕过 arguments_rfs2971.
        # Gmail / Outlook / QQ 不要求但接受这种格式, 兼容性无害.
        try:
            from aioimaplib.aioimaplib import Command
            notify = s.notify_to or s.imap_user
            cmd = Command(
                "ID", client.protocol.new_tag(),
                '("name"', '"GrowthPress"',
                '"version"', '"0.2.0"',
                '"vendor"', '"GrowthPress AI"',
                '"support-email"', f'"{notify}")',
                loop=asyncio.get_event_loop(),
            )
            id_resp = await client.protocol.execute(cmd)
            if id_resp.result != "OK":
                log.warning(f"[poller] IMAP ID 非 OK ({id_resp.result}): {id_resp.lines}, 继续试 SELECT")
        except Exception as e:
            log.warning(f"[poller] IMAP ID 失败 (继续 SELECT 试试): {e!r}")

        resp = await client.select(s.imap_folder)
        if resp.result != "OK":
            raise RuntimeError(f"IMAP select {s.imap_folder} failed: {resp.lines}")

        # search UNSEEN — 拿 UID 列表
        resp = await client.uid_search("UNSEEN") if hasattr(client, "uid_search") \
            else await client.uid("search", "UNSEEN")
        if resp.result != "OK":
            log.warning(f"[poller] search UNSEEN 失败: {resp.lines}")
            return 0

        # resp.lines[0] 形如 b'1 2 3' (UID 列表) 或 b'' (无未读)
        uid_line = resp.lines[0] if resp.lines else b""
        if isinstance(uid_line, bytes):
            uid_line = uid_line.decode("ascii", errors="ignore")
        uid_strs = uid_line.split()
        if not uid_strs:
            log.debug("[poller] 无未读邮件")
            return 0

        log.info(f"[poller] 发现 {len(uid_strs)} 封未读邮件: uids={uid_strs}")
        processed = 0
        for uid_s in uid_strs:
            try:
                uid = int(uid_s)
            except ValueError:
                continue
            try:
                await _fetch_and_dispatch(client, uid, db)
                processed += 1
            except Exception as e:
                log.error(
                    f"[poller] uid={uid} fetch/dispatch 失败: {e!r}",
                    exc_info=True,
                )
                # 单封失败不标 \\Seen, 下轮重试
        return processed
    finally:
        try:
            await client.logout()
        except Exception as e:
            log.debug(f"[poller] logout 异常 (忽略): {e!r}")


async def _fetch_and_dispatch(
    client: Any, uid: int, db: Database
) -> None:
    """fetch 单封 → parse → dispatch → STORE \\Seen.

    fetch 用 RFC822 (整封原文), 简化解析. UID-based 命令防 reindex 问题.
    """
    # fetch RFC822 — aioimaplib uid("fetch", uid, "(RFC822)")
    resp = await client.uid("fetch", str(uid), "(RFC822)")
    if resp.result != "OK":
        raise RuntimeError(f"uid fetch {uid} failed: {resp.lines}")

    # resp.lines 形如:
    #   [b'1 FETCH (UID 1 RFC822 {1234}', b'<raw bytes ...>', b')', b'OK ...']
    raw_bytes: bytes | None = None
    for line in resp.lines:
        if isinstance(line, (bytes, bytearray)) and len(line) > 200:
            raw_bytes = bytes(line)
            break
    if raw_bytes is None:
        # 兜底: 找最长的 bytes 块
        candidates = [bytes(l) for l in resp.lines if isinstance(l, (bytes, bytearray))]
        if candidates:
            raw_bytes = max(candidates, key=len)

    if not raw_bytes:
        log.warning(f"[poller] uid={uid} RFC822 内容为空, 跳过")
        return

    msg = _parse_rfc822(raw_bytes, uid=uid)
    if msg is None:
        log.warning(f"[poller] uid={uid} parse 返回 None, 跳过")
        return

    # dispatch (任何 handler 异常向上抛, 由 _poll_once 单封 try 包)
    await dispatch_inbound(msg, db)

    # 处理成功 → STORE \\Seen 防重复
    resp = await client.uid("store", str(uid), "+FLAGS", r"(\Seen)")
    if resp.result != "OK":
        log.warning(f"[poller] uid={uid} STORE \\Seen 失败: {resp.lines}")
    else:
        log.debug(f"[poller] uid={uid} 标记 \\Seen ok")


# ---------- 长生命周期 task ----------

async def imap_poller_run(db: Database) -> None:
    """主入口. 每 POLL_INTERVAL_SEC 秒扫一次. 异常永不挂 task.

    orchestrator.py 的 TaskGroup 调用. 替换原 stub 时:
      tg.create_task(imap_poller_run(db), name="imap_poller")
    """
    s = get_settings()
    if not s.imap_user or not s.imap_pass.get_secret_value():
        log.warning(
            "[poller] IMAP 未配置 (imap_user / imap_pass 空), task exit. "
            "在 .env 配 IMAP_USER / IMAP_PASS 后重启 daemon"
        )
        return

    log.info(
        f"[poller] 启动: {s.imap_host}:{s.imap_port} {s.imap_user!r} "
        f"folder={s.imap_folder!r} interval={POLL_INTERVAL_SEC}s"
    )

    while True:
        try:
            n = await _poll_once(db)
            if n:
                log.info(f"[poller] 本轮处理 {n} 封")
        except asyncio.CancelledError:
            log.info("[poller] cancelled, 退出")
            raise
        except Exception as e:
            log.error(f"[poller] 本轮失败 (等下轮重试): {e!r}", exc_info=True)
        await asyncio.sleep(POLL_INTERVAL_SEC)


__all__ = ["imap_poller_run", "POLL_INTERVAL_SEC"]
