"""m2 reviewer 输出 schema. 各 check_* 函数返回值的 pydantic 校验."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, Field


class ComplianceResult(BaseModel):
    passed: bool
    severity: Literal["ok", "warn", "critical"] = "ok"
    issues: list[str] = Field(default_factory=list)


class QualityResult(BaseModel):
    passed: bool
    score: int = Field(ge=0, le=100)
    issues: list[str] = Field(default_factory=list)
    suggested_edits: list[str] = Field(default_factory=list)


class PlatformResult(BaseModel):
    passed: bool
    # key 是平台名 (juejin / xiaohongshu / csdn / zhihu), value 是该平台的问题列表
    issues_by_platform: dict[str, list[str]] = Field(default_factory=dict)
    suggested_edits: list[str] = Field(default_factory=list)


@dataclass
class Verdict:
    """三路合并结果, m2 最终决策依据."""
    passed: bool                      # 三路全 True 才 True
    compliance: ComplianceResult
    quality: QualityResult | None      # 合规挂时 None
    platform: PlatformResult | None    # 合规挂时 None
    all_issues: list[str] = field(default_factory=list)
