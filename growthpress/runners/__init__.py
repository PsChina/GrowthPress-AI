"""Python 派工给外部 claude CLI 的 runner.

新架构: daemon 是"老板"派工 + watch 邮箱; Claude CLI 是"工人"短命跑任务.
runner 封装 subprocess.create_subprocess_exec + timeout + 杀进程组.
"""
from .claude_subprocess import ClaudeResult, run_claude
from .email_chat_dispatcher import email_chat_dispatcher_run
from .revising_dispatcher import revising_dispatcher_run

__all__ = [
    "ClaudeResult",
    "run_claude",
    "email_chat_dispatcher_run",
    "revising_dispatcher_run",
]
