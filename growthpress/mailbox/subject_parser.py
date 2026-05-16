"""m5 subject parser — 识别 APV / PUB / REJECT / ROUTE-* 前缀.

subject 形态:
  - 出邮件: `[APV-7f3a2b] 待确认: ...`
  - 入回信: `Re: [APV-7f3a2b] 待确认: ...` (Gmail/Outlook 客户端添加 "Re: ")
  - 路线管理: `[ROUTE-ADD]` / `[ROUTE-REMOVE]` / `[ROUTE-LIST]` / `[ROUTE-DEFAULT]` / `[ROUTE-CRON]`

所有解析返回 SubjectKind enum + 可选的 token (APV/PUB 的 id), 让 dispatcher 直接 dispatch.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum


class SubjectKind(str, Enum):
    APPROVAL_REPLY = "approval_reply"   # [APV-<id>]
    RETRACT_REPLY = "retract_reply"     # [PUB-<id>]
    REJECT_NOTICE = "reject_notice"     # [REJECT-<id>] — 出邮件方向, 入到不该处理
    ROUTE_ADD = "route_add"
    ROUTE_REMOVE = "route_remove"
    ROUTE_LIST = "route_list"
    ROUTE_DEFAULT = "route_default"
    ROUTE_CRON = "route_cron"
    UNKNOWN = "unknown"                 # 无 token, 走启发式 dispatch


@dataclass(frozen=True)
class SubjectInfo:
    kind: SubjectKind
    token: str = ""        # APV/PUB/REJECT 后面的 id (uuid 前 6-8 位)
    raw: str = ""          # 原 subject (debug 用)


# token 形态: 字母 / 数字 / 下划线 / 连字符. 长度 4-32 (uuid 前 6 位是 6, 留余量).
_TOKEN = r"([A-Za-z0-9_\-]{4,32})"

_RE_APV = re.compile(rf"\[APV-{_TOKEN}", re.IGNORECASE)
_RE_PUB = re.compile(rf"\[PUB-{_TOKEN}", re.IGNORECASE)
_RE_REJECT = re.compile(rf"\[REJECT-{_TOKEN}", re.IGNORECASE)

# ROUTE-* 命令不带 token. 严格按列表识别, 防止 [ROUTE-XXX] 乱写也匹配上.
_RE_ROUTE_ADD = re.compile(r"\[ROUTE-ADD\]", re.IGNORECASE)
_RE_ROUTE_REMOVE = re.compile(r"\[ROUTE-REMOVE\]", re.IGNORECASE)
_RE_ROUTE_LIST = re.compile(r"\[ROUTE-LIST\]", re.IGNORECASE)
_RE_ROUTE_DEFAULT = re.compile(r"\[ROUTE-DEFAULT\]", re.IGNORECASE)
_RE_ROUTE_CRON = re.compile(r"\[ROUTE-CRON\]", re.IGNORECASE)


def parse_subject(subject: str) -> SubjectInfo:
    """识别 subject 类型. 优先级: APV > PUB > REJECT > ROUTE-* > UNKNOWN.

    Gmail 转发 / Re: 前缀都被 fragment 匹配吃掉 (re.search 不锚定开头), 所以
    `Re: Fwd: [APV-xxx] ...` 也能命中.
    """
    if not subject:
        return SubjectInfo(kind=SubjectKind.UNKNOWN, raw="")

    if m := _RE_APV.search(subject):
        return SubjectInfo(kind=SubjectKind.APPROVAL_REPLY, token=m.group(1), raw=subject)
    if m := _RE_PUB.search(subject):
        return SubjectInfo(kind=SubjectKind.RETRACT_REPLY, token=m.group(1), raw=subject)
    if m := _RE_REJECT.search(subject):
        # REJECT 是出邮件方向, 用户改后应发新邮件而不是回复. 收到 REJECT 入信 = 用户回了
        # 本不该回的邮件 → 当 UNKNOWN 走启发式 (大概率被判成 topic / content) 或忽略.
        return SubjectInfo(kind=SubjectKind.REJECT_NOTICE, token=m.group(1), raw=subject)

    if _RE_ROUTE_ADD.search(subject):
        return SubjectInfo(kind=SubjectKind.ROUTE_ADD, raw=subject)
    if _RE_ROUTE_REMOVE.search(subject):
        return SubjectInfo(kind=SubjectKind.ROUTE_REMOVE, raw=subject)
    if _RE_ROUTE_LIST.search(subject):
        return SubjectInfo(kind=SubjectKind.ROUTE_LIST, raw=subject)
    if _RE_ROUTE_DEFAULT.search(subject):
        return SubjectInfo(kind=SubjectKind.ROUTE_DEFAULT, raw=subject)
    if _RE_ROUTE_CRON.search(subject):
        return SubjectInfo(kind=SubjectKind.ROUTE_CRON, raw=subject)

    return SubjectInfo(kind=SubjectKind.UNKNOWN, raw=subject)


__all__ = ["SubjectKind", "SubjectInfo", "parse_subject"]
