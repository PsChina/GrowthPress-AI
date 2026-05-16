"""行程守护第 5 层 — 日报邮件 [DAILY-yyyy-mm-dd].

按 memory architecture.md / 行程守护 / 第 5 层 设计.

公开 API:
    daily_digest_task(db) — 长生命周期 task, 每天 UTC 0:00 (北京 8:00) 发一封统计邮件
"""
from .agent import daily_digest_task

__all__ = ["daily_digest_task"]
