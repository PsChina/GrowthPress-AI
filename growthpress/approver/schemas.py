"""m3 approver 输出 schema. parse_reply() 返回的 Action 类型, 以及内部辅助 dataclass.

Action 是 sum type (4 个变体), 用 dataclass + isinstance 分派, 不用 pydantic
(纯内存对象, 不入库 / 不出网, 用 dataclass 更轻).
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Approve:
    """回信首词 ok / yes / 通过 / 发 → 通过.

    platforms = "all" 表示走默认全平台; 否则是 csv 字符串 (e.g. "juejin,csdn").
    存进 approvals.platforms 字段 / 后续传给 m4.
    """
    platforms: str = "all"


@dataclass
class Reject:
    """回信首词 改 / no / 退 → 退回改稿.

    feedback 是后续文本 (用户修改意见), 落进 approvals.feedback,
    后续传给 m1 / writer 让它带 issues 改.
    """
    feedback: str = ""


@dataclass
class Drop:
    """回信首词 drop / 丢 / 弃 → 直接丢弃归档."""
    pass


@dataclass
class Unknown:
    """首词不识别 — 不改 state, 回信一封 "未识别" 提示.

    raw 保留原首词 + 部分上下文, 用于回信引用 (方便用户看自己写了什么).
    """
    raw: str = ""


# Action sum type: handler 用 isinstance 分派
Action = Approve | Reject | Drop | Unknown


__all__ = ["Approve", "Reject", "Drop", "Unknown", "Action"]
