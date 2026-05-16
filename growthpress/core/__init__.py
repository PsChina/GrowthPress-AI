"""m0 core — 全局配置.

新架构: GrowthPress 不再内置 LLM 调用 (旧 llm_client / llm_router 已删).
"大脑"由外部 claude CLI 担任. 本包只导出 Settings 给其他模块共享.

公开 API:
  Settings         — pydantic-settings 类 (类型提示用)
  get_settings()   — 拿单例 Settings (lru_cache)
"""
from .settings import Settings, get_settings

__all__ = ["Settings", "get_settings"]
