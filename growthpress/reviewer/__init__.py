"""m2 reviewer — 合规 / 质量 / 平台 三路审核.

公开 API:
    review(draft_id, db, round_=1) -> Verdict | None
    Verdict / ComplianceResult / QualityResult / PlatformResult
"""
from .agent import (
    check_compliance,
    check_platforms,
    check_quality,
    review,
)
from .schemas import (
    ComplianceResult,
    PlatformResult,
    QualityResult,
    Verdict,
)

__all__ = [
    "review",
    "check_compliance",
    "check_quality",
    "check_platforms",
    "Verdict",
    "ComplianceResult",
    "QualityResult",
    "PlatformResult",
]
