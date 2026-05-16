"""m0 core — 全局配置 + LLM client.

公开 API:
  Settings         — pydantic-settings 类 (类型提示用)
  get_settings()   — 拿单例 Settings (lru_cache)
  get_llm_client() — 拿单例 AsyncOpenAI client (按 .env 配置)
"""
from .settings import Settings, get_settings
from .llm_client import get_llm_client

__all__ = ["Settings", "get_settings", "get_llm_client"]
