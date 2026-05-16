"""m2-m5 共用: 任务级模型路由 + 调用统计 (经济性策略).

按 architecture memory 'LLM 模型路由 + 经济性' 段实现.

V1 范围:
  - 硬映射 Task → 逻辑模型 (flash / pro), 不实装升级 (留 hook)
  - 调用统计 → llm_calls 表 (token / duration / 成功失败)
  - 升级 trigger / 月预算保护 → W3+ 再加

用法:
    from growthpress.core.llm_router import Task, call

    result = await call(
        task=Task.REVIEW_QUALITY,
        db=db,
        draft_id="abc123",
        system=PROMPT,
        messages=[{"role": "user", "content": "..."}],
        max_tokens=2048,
    )
    print(result.text, result.model_used, result.input_tokens)
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import TYPE_CHECKING, Any

from .llm_client import get_llm_client
from .settings import get_settings

if TYPE_CHECKING:
    from ..db import Database

log = logging.getLogger("growthpress.llm_router")


class Task(str, Enum):
    """LLM 任务标签. 决定 router 选哪个模型 + llm_calls 表用什么 task 字段."""
    SCOUT_RESEARCH    = "scout_research"
    SCOUT_WRITING     = "scout_writing"
    REVIEW_COMPLIANCE = "review_compliance"
    REVIEW_QUALITY    = "review_quality"
    REVIEW_PLATFORM   = "review_platform"
    EMAIL_CLASSIFY    = "email_classify"
    REVISE            = "revise"


# 按 architecture memory 表. 改这里 = 全局生效.
MODEL_FOR_TASK: dict[Task, str] = {
    Task.SCOUT_RESEARCH:    "flash",
    Task.SCOUT_WRITING:     "pro",
    Task.REVIEW_COMPLIANCE: "flash",
    Task.REVIEW_QUALITY:    "pro",
    Task.REVIEW_PLATFORM:   "flash",
    Task.EMAIL_CLASSIFY:    "flash",
    Task.REVISE:            "pro",
}


@dataclass(frozen=True)
class LLMResult:
    text: str                # 拼接所有 text block (跳过 thinking / tool_use)
    model_used: str          # 逻辑名: "flash" / "pro" / "claude"
    model_id: str            # 实际模型 ID, 如 "deepseek-v4-flash[1m]"
    upgraded_from: str | None
    input_tokens: int
    output_tokens: int
    duration_ms: int


def _resolve_model_id(model_logical: str) -> str:
    """逻辑名 → .env 配置的实际模型 ID."""
    s = get_settings()
    if model_logical == "flash":
        return s.llm_model_flash
    if model_logical == "pro":
        return s.llm_model_pro
    raise ValueError(f"未知逻辑模型名: {model_logical!r}")


async def call(
    task: Task,
    *,
    messages: list[dict[str, Any]],
    system: str | None = None,
    tools: list[dict[str, Any]] | None = None,
    max_tokens: int = 4096,
    db: "Database | None" = None,
    draft_id: str | None = None,
    _force_model: str | None = None,    # 内部用 (升级路径), 调用方一般不传
) -> LLMResult:
    """统一 LLM 调用入口.

    流程:
      1. 按 task 查 MODEL_FOR_TASK (`_force_model` 可覆盖)
      2. anthropic streaming 调用 (long-running 稳, 避免 idle close)
      3. 提取 text + 算 tokens (final.usage)
      4. 写 llm_calls 表 (db 传入时, 失败/成功都记)

    异常处理: 不吞异常, 调用方自己 try (router 只记 llm_calls 然后 re-raise).
    """
    model_logical = _force_model or MODEL_FOR_TASK.get(task)
    if model_logical is None:
        raise ValueError(f"无 model 映射: task={task}")
    model_id = _resolve_model_id(model_logical)

    client = get_llm_client()
    t_start = time.monotonic()
    success = False
    error_msg: str | None = None
    text_parts: list[str] = []
    input_tokens = 0
    output_tokens = 0

    log.info(f"[router] {task.value} → {model_logical} ({model_id})")
    try:
        kwargs: dict[str, Any] = {
            "model": model_id,
            "max_tokens": max_tokens,
            "messages": messages,
        }
        if system is not None:
            kwargs["system"] = system
        if tools:
            kwargs["tools"] = tools

        async with client.messages.stream(**kwargs) as stream:
            async for _ in stream:
                pass
            final = await stream.get_final_message()

        for block in final.content:
            if getattr(block, "type", None) == "text":
                text_parts.append(block.text)

        if final.usage:
            input_tokens = final.usage.input_tokens or 0
            output_tokens = final.usage.output_tokens or 0

        success = True
    except Exception as e:
        error_msg = f"{type(e).__name__}: {e}"
        log.error(f"[router] {task.value} failed: {error_msg}")
        raise
    finally:
        duration_ms = int((time.monotonic() - t_start) * 1000)
        # 写 llm_calls (不影响主流程, 自己 try)
        if db is not None:
            try:
                await db.write(
                    """INSERT INTO llm_calls
                       (draft_id, task, model, model_id, input_tokens, output_tokens,
                        duration_ms, success, upgraded_from, error, at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        draft_id, task.value, model_logical, model_id,
                        input_tokens or None, output_tokens or None,
                        duration_ms, success, None, error_msg,
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )
            except Exception as log_err:
                log.error(f"[router] llm_calls 写入失败 (不影响 LLM 结果): {log_err}")

    return LLMResult(
        text="".join(text_parts),
        model_used=model_logical,
        model_id=model_id,
        upgraded_from=None,        # V1 不实装升级
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        duration_ms=duration_ms,
    )
