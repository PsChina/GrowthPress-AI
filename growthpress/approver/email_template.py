"""m3 approver 邮件模板: APV 邮件 subject / body + Unknown 回信提示.

模板来源 [[project-growthpress-human-loop]] L130-L150. 保持纯函数, 无 IO,
方便单测.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

# 默认计划平台 — 与 publisher / web-publish CLI 对齐.
# m3 只展示给用户看 (让用户决定要不要在回信里指定子集), 不实际控制 publish.
DEFAULT_PLATFORMS = ("掘金", "CSDN", "知乎", "小红书")


def build_apv_subject(pub_id: str, title: str) -> str:
    """APV 邮件 subject. 含 pub_id token 让 IMAP poller 能从 Re: ... 抽回来."""
    return f"[APV-{pub_id}] 待确认: 《{title}》"


def build_apv_body(
    *,
    pub_id: str,
    title: str,
    summary: str,
    body_md: str,
    draft_path: Path | str,
    expires_at: datetime,
    platforms: tuple[str, ...] = DEFAULT_PLATFORMS,
) -> str:
    """APV 邮件正文 (plain text).

    预览取 body_md 首 300 字 (markdown 原样, 不渲染 — 邮件客户端不一定支持).
    draft_path 用 file:// URI 让用户本地点开看完整版.
    """
    preview = (body_md or "").strip()[:300]
    if not preview:
        preview = "(无正文预览)"

    # 转 file:// URI (绝对路径)
    abs_path = Path(draft_path).resolve()
    draft_uri = f"file://{abs_path}"

    expires_str = expires_at.strftime("%Y-%m-%d %H:%M")
    platforms_str = ", ".join(platforms)

    return f"""────────────────────────────────────
标题: 《{title}》
摘要: {summary or "(无摘要)"}

预览 (首 300 字):
{preview}

完整草稿: {draft_uri}
计划平台: {platforms_str}
失效时间: {expires_str} (24h 后挂起, 需手动恢复)
────────────────────────────────────

回复操作 (首词识别, 大小写不敏感):
  ok                  → 全平台发
  ok juejin csdn      → 只发指定平台 (其余跳过)
  改 <修改意见>        → 退回改稿 (issues 传给 Writer, 重启 ≤2 轮)
  drop                → 丢弃归档
  其他                → 回一封 "未识别" 提示

本邮件由 GrowthPress AI 自动发出. pub_id={pub_id}
"""


def build_unknown_subject(pub_id: str, original_subject: str = "") -> str:
    """Unknown 回信: 提示用户回复未识别. 保持 pub_id token, 用户可以再回一次."""
    if original_subject:
        # 已含 Re: 不重复加
        if original_subject.lower().startswith("re:"):
            return f"{original_subject} (未识别)"
        return f"Re: {original_subject} (未识别)"
    return f"[APV-{pub_id}] 未识别的回复"


def build_unknown_body(*, pub_id: str, raw: str) -> str:
    """Unknown 回信正文: 列出有效指令 + 引用用户原首词."""
    raw_quote = raw or "(空)"
    return f"""您的回复 "{raw_quote}" 未被识别.

请直接回复以下指令之一 (首词识别, 大小写不敏感):
  ok                  → 全平台发
  ok juejin csdn      → 只发指定平台
  改 <修改意见>        → 退回改稿
  drop                → 丢弃归档

本审核请求仍处于 pending 状态, 24h 内可继续回复.

本邮件由 GrowthPress AI 自动发出. pub_id={pub_id}
"""


__all__ = [
    "build_apv_subject",
    "build_apv_body",
    "build_unknown_subject",
    "build_unknown_body",
    "DEFAULT_PLATFORMS",
]
