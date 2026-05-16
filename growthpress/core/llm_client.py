"""m0 core: 统一 LLM client (走 Anthropic SDK + 协议).

⭐ 关键架构: DeepSeek 实现了 Anthropic 兼容端点 → 用 anthropic SDK 走 DeepSeek
后端, web_search server tool 由 DeepSeek 实现 (免费, 跟 DeepSeek 套餐计费).

为什么不用 openai SDK + OpenAI 协议:
  - Anthropic 的 web_search server tool 是 Anthropic spec 的一部分
  - DeepSeek 兼容 Anthropic spec → 兼容 web_search
  - openai SDK + DeepSeek 走 OpenAI 协议, 没有 web_search server tool (要自己接 Tavily 等)
  - 所以即使后端是 DeepSeek, 也要用 anthropic SDK + Anthropic 协议

切换:
  .env LLM_BASE_URL=https://api.deepseek.com/anthropic  → DeepSeek 接管 (默认, 便宜+免费 web_search)
  .env LLM_BASE_URL=https://api.anthropic.com          → 真 Anthropic (按 token 计费, $10/1000 web_search)

用法:
  from growthpress.core import get_llm_client, get_settings
  client = get_llm_client()
  s = get_settings()
  resp = await client.messages.create(
      model=s.llm_model,
      tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 5}],
      max_tokens=4096,
      messages=[{"role": "user", "content": "..."}]
  )
"""
from __future__ import annotations

from functools import lru_cache

from anthropic import AsyncAnthropic

from .settings import get_settings


@lru_cache
def get_llm_client() -> AsyncAnthropic:
    """单例 AsyncAnthropic client. 跨模块共享, 内部连接池复用."""
    s = get_settings()
    return AsyncAnthropic(
        api_key=s.llm_api_key.get_secret_value() or "placeholder",
        base_url=s.llm_base_url,
    )
