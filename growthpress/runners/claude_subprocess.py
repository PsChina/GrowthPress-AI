"""起一个非交互的 claude CLI subprocess 跑任务, 带 timeout + 杀进程组.

为什么需要这层:
  GrowthPress daemon (Python) 是"老板", Claude CLI 是"工人". daemon watch 邮箱
  收到命令 / APV 回信 "改 X" 后, 起 claude subprocess 让 Claude 跑改稿 / 发文
  工作流. Python 这边只负责派工 + 监控.

设计:
  - 子进程跑 `claude -p "<prompt>" --output-format json --no-session-persistence`
  - 后台无人审批权限 → `--dangerously-skip-permissions` 必加 (有风险, 只用于可信任
    cwd + 可信任 prompt)
  - asyncio.wait_for + os.killpg 杀整个进程组 (claude 可能 spawn 工具子进程)
  - 输出落 logs/runs/<run_id>/{stdout,stderr,prompt}.log 留档审计
  - 退出码 0 = 成功, 其他 = 失败 (claude CLI 约定)

不做:
  - --max-budget-usd 防失控 — Anthropic Max OAuth 套餐不走 API key 计费, 设了也没用
  - 嵌套 subprocess (claude 内部又起 claude) — 当前不需要, watchdog 跟踪不便
  - 自动重试 — 失败留人工处理, 别盲跑烧 quota
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import signal
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("growthpress.runners.claude")

# 仓库根 — daemon 一般在这里起, claude subprocess 也用这个作 cwd
_REPO_ROOT = Path(__file__).resolve().parents[2]
_LOG_DIR = _REPO_ROOT / "logs" / "claude-runs"

DEFAULT_TIMEOUT_SEC = 1800   # 30 分钟硬上限. 改稿/发文一般 5-10 分钟内


@dataclass(frozen=True)
class ClaudeResult:
    """Claude subprocess 跑完的产物."""
    run_id: str               # 本次跑的 uuid (= claude --session-id), log 落盘用
    success: bool             # exit_code == 0
    exit_code: int | None     # None 表示被 kill (timeout)
    elapsed_sec: float
    stdout_path: Path         # 全 stdout 落盘
    stderr_path: Path
    prompt_path: Path         # 原始 prompt 留底
    timed_out: bool           # True = 命中 timeout 被杀
    kill_signal: int | None   # 实际发的信号 (SIGTERM / SIGKILL)


def _find_claude_bin() -> str:
    """找 claude CLI 路径. PATH 优先, 不在则报错让 daemon 知道."""
    p = shutil.which("claude")
    if p:
        return p
    # 用户配置常见路径兜底
    for cand in (
        Path.home() / ".local" / "bin" / "claude",
        Path("/usr/local/bin/claude"),
        Path("/opt/homebrew/bin/claude"),
    ):
        if cand.is_file() and os.access(cand, os.X_OK):
            return str(cand)
    raise FileNotFoundError(
        "claude CLI 不在 PATH 也不在常见路径. "
        "确认安装了 Claude Code, launchd plist 加 PATH=/Users/<u>/.local/bin:$PATH"
    )


async def _wait_with_kill(
    proc: asyncio.subprocess.Process,
    timeout_sec: float,
) -> tuple[bool, int | None]:
    """等 proc 完成. 超时 → SIGTERM, 还不退 → SIGKILL. 杀进程组防 zombie 子进程.

    返回 (timed_out, kill_signal). kill_signal None 表示正常完成没杀.
    """
    try:
        await asyncio.wait_for(proc.wait(), timeout=timeout_sec)
        return (False, None)
    except asyncio.TimeoutError:
        log.warning(f"[claude-runner] pid={proc.pid} 超时 {timeout_sec}s, SIGTERM")
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError) as e:
            log.warning(f"[claude-runner] SIGTERM 杀进程组失败: {e!r}")
        # 给 5s 优雅退出, 不退就 SIGKILL
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
            return (True, signal.SIGTERM)
        except asyncio.TimeoutError:
            log.warning(f"[claude-runner] pid={proc.pid} SIGTERM 后仍卡, SIGKILL")
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=3)
            except asyncio.TimeoutError:
                pass
            return (True, signal.SIGKILL)


async def run_claude(
    prompt: str,
    *,
    cwd: Path | None = None,
    timeout_sec: float = DEFAULT_TIMEOUT_SEC,
    model: str = "sonnet",
    extra_args: list[str] | None = None,
) -> ClaudeResult:
    """起一个 non-interactive claude CLI 子进程跑 prompt.

    Args:
        prompt: 任务 prompt. daemon 拼好的 (例: '改稿 draft xxx, 反馈是 ...').
        cwd: 工作目录, 默认 GrowthPress-AI 仓库根.
        timeout_sec: 硬超时. 超过就 SIGTERM, 5s 不退 SIGKILL.
        model: 'sonnet' / 'opus' / 'haiku' 或完整 ID. 默认 sonnet 省钱.
        extra_args: 透传给 claude 的额外参数 (例 ['--add-dir', '/tmp']).

    Returns:
        ClaudeResult — 不抛, 失败状态在 success/timed_out 里看.
    """
    bin_path = _find_claude_bin()
    cwd = cwd or _REPO_ROOT
    run_id = uuid.uuid4().hex[:12]
    run_dir = _LOG_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    prompt_file = run_dir / "prompt.txt"
    stdout_file = run_dir / "stdout.log"
    stderr_file = run_dir / "stderr.log"
    prompt_file.write_text(prompt, encoding="utf-8")

    # claude --session-id 必须是 UUID 格式 (带 dash). 用 uuid4 完整 36 字符.
    session_uuid = str(uuid.UUID(run_id + "0" * (32 - len(run_id))))

    cmd = [
        bin_path,
        "-p", prompt,
        "--output-format", "json",
        "--no-session-persistence",
        "--dangerously-skip-permissions",   # 后台无人审权限弹窗, 必加
        "--model", model,
        "--session-id", session_uuid,
    ]
    if extra_args:
        cmd.extend(extra_args)

    log.info(
        f"[claude-runner] start run_id={run_id} cwd={cwd} model={model} "
        f"timeout={timeout_sec}s prompt={prompt[:80]!r}..."
    )
    t_start = time.monotonic()

    # start_new_session=True → setsid, 给整个进程组一个新 sid, killpg 才能杀整组
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(cwd),
        stdout=stdout_file.open("wb"),
        stderr=stderr_file.open("wb"),
        start_new_session=True,
    )

    timed_out, kill_sig = await _wait_with_kill(proc, timeout_sec)
    elapsed = time.monotonic() - t_start

    log.info(
        f"[claude-runner] done run_id={run_id} exit={proc.returncode} "
        f"elapsed={elapsed:.1f}s timed_out={timed_out}"
    )

    return ClaudeResult(
        run_id=run_id,
        success=(proc.returncode == 0 and not timed_out),
        exit_code=proc.returncode,
        elapsed_sec=elapsed,
        stdout_path=stdout_file,
        stderr_path=stderr_file,
        prompt_path=prompt_file,
        timed_out=timed_out,
        kill_signal=kill_sig,
    )
