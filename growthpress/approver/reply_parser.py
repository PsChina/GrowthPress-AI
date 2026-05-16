"""m3 approver 回信解析: 纯字符串处理, 无 IO / 无 async.

设计参考 [[project-growthpress-human-loop]] L141-L155 — 首词识别, 大小写不敏感.
失败模式偏 "Unknown 回提示信", 不要瞎猜 (静默错决策风险远高于多发一封提示信).
"""
from __future__ import annotations

import re

from .schemas import Action, Approve, Drop, Reject, Unknown

# 平台白名单 (与 publisher / web-publish CLI 对齐).
# 未来加平台改这一行, 不要散落各处.
KNOWN_PLATFORMS: tuple[str, ...] = ("juejin", "csdn", "zhihu", "xiaohongshu")

# 首词分类: 中英混合, 大小写不敏感 (lower 后比对).
_APPROVE_HEADS = {"ok", "yes", "通过", "发", "approve"}
_REJECT_HEADS  = {"改", "no", "退", "reject"}
_DROP_HEADS    = {"drop", "丢", "弃", "archive"}

# 邮件回复常见尾巴 (quoted block / signature) — 取第一行非空 + 在 quote 之前.
_QUOTE_PREFIX_RE = re.compile(r"^[>|]")


def _strip_quotes_and_signature(body: str) -> str:
    """剥邮件 quoted history (> 开头) + 常见签名分隔 (-- ).

    只保留 quote 之前的"用户实际写的内容". 邮件客户端引用历史会让回信变长,
    parse 首词时要先剥掉, 否则首词容易抓到 "On 2026-05-16 ... wrote:".
    """
    lines: list[str] = []
    for line in body.splitlines():
        stripped = line.lstrip()
        # 签名分隔: "-- " (RFC 3676) → 后面全是签名
        if stripped == "-- ":
            break
        # quoted: "> ..." 或 "| ..." 或 "On ... wrote:"
        if _QUOTE_PREFIX_RE.match(stripped):
            break
        # Gmail "On Wed, May 16, 2026 at ... wrote:" — 启发式匹配
        if re.match(r"^On .{5,80} wrote:\s*$", stripped):
            break
        lines.append(line)
    return "\n".join(lines).strip()


def _parse_platforms(rest: str) -> str:
    """从 "ok juejin csdn" 的 rest 部分抽平台 csv. 不识别的平台名忽略.

    返回 "all" 表示用户没指定 / 全部不识别 (兜底走全平台, 后续 m4 用默认列表).
    """
    tokens = [t.lower().strip(",.;") for t in rest.split() if t.strip()]
    matched = [t for t in tokens if t in KNOWN_PLATFORMS]
    if not matched:
        return "all"
    # 去重保序
    seen: set[str] = set()
    out: list[str] = []
    for t in matched:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return ",".join(out)


def parse_reply(body: str) -> Action:
    """解析回信正文 → Action.

    入参: 邮件 plain text body (handler 已从 multipart / html 抽出 plain text).
    出参: Approve / Reject / Drop / Unknown 之一.

    规则 (memory human_loop.md L141-L155):
      首词 ok / yes / 通过 / 发     → Approve(platforms=后续平台 csv 或 "all")
      首词 改 / no / 退             → Reject(feedback=后续全部文本)
      首词 drop / 丢 / 弃           → Drop()
      其他 / 空 / 无法分类           → Unknown(raw=首行截断)
    """
    if not body or not body.strip():
        return Unknown(raw="")

    cleaned = _strip_quotes_and_signature(body)
    if not cleaned:
        return Unknown(raw="")

    # 取第一个非空行作为指令行 — 兼容用户在第一行直接写指令的常见习惯.
    first_line = next((ln.strip() for ln in cleaned.splitlines() if ln.strip()), "")
    if not first_line:
        return Unknown(raw="")

    parts = first_line.split(maxsplit=1)
    head = parts[0].lower()
    rest = parts[1] if len(parts) > 1 else ""

    if head in _APPROVE_HEADS:
        return Approve(platforms=_parse_platforms(rest))

    if head in _REJECT_HEADS:
        # Reject 的 feedback 应该包括 cleaned 全部 (rest + 后续行), 用户可能多行写意见
        # 第一行去掉首词后剩 rest, 加上后续行
        first_line_rest = rest.strip()
        other_lines = cleaned.split("\n", 1)
        feedback_parts: list[str] = []
        if first_line_rest:
            feedback_parts.append(first_line_rest)
        if len(other_lines) > 1:
            tail = other_lines[1].strip()
            if tail:
                feedback_parts.append(tail)
        return Reject(feedback="\n".join(feedback_parts).strip())

    if head in _DROP_HEADS:
        return Drop()

    # 截首 80 字回填给 Unknown — 后续 handler 回信时引用让用户知道写了啥
    return Unknown(raw=first_line[:80])


__all__ = ["parse_reply", "KNOWN_PLATFORMS"]
