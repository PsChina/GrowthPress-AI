"""m0 core: 统一 LLM client (OpenAI 协议兼容).

DeepSeek (默认) 和 Anthropic 官方都提供 OpenAI 兼容端点 → 一个 AsyncOpenAI client
配不同 base_url 即可切. 调用方代码不变.

切换:
  .env 改 LLM_BASE_URL=https://api.deepseek.com (默认) → DeepSeek
  .env 改 LLM_BASE_URL=https://api.anthropic.com/v1 → Anthropic OpenAI 兼容
  对应换 LLM_API_KEY 和 LLM_MODEL

⚠ Anthropic OpenAI 兼容端点功能有限 (web_search server tool 等高级 feature 不可用).
全栈 DeepSeek 是当前决策 ([[project-growthpress-overview]] L135), Anthropic 仅 fallback.

用法:
  from growthpress.core import get_llm_client
  client = get_llm_client()
  resp = await client.chat.completions.create(model=settings.llm_model, messages=[...])
"""
from __future__ import annotations

from functools import lru_cache

from openai import AsyncOpenAI

from .settings import get_settings


@lru_cache
def get_llm_client() -> AsyncOpenAI:
    """单例 AsyncOpenAI client. 跨模块共享, 内部连接池复用."""
    s = get_settings()
    return AsyncOpenAI(
        api_key=s.llm_api_key.get_secret_value() or "placeholder",
        base_url=s.llm_base_url,
    )
