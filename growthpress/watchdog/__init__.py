"""行程守护第 3 层 — state 卡死检测.

按 memory architecture.md / 行程守护 / 第 3 层 设计.

公开 API:
    watchdog_task(db) — 长生命周期 task, orchestrator 挂进 TaskGroup
    THRESHOLDS        — state → timeout/action 表, 改这个 = 全局生效
"""
from .agent import THRESHOLDS, watchdog_task

__all__ = ["watchdog_task", "THRESHOLDS"]
