"""m0 core: 全局配置 (pydantic-settings 读 .env).

字段分组:
  LLM_*       — Scout+Writer + Reviewer 用的 LLM (默认 DeepSeek)
  SMTP_*      — m3/m4/m5 发邮件
  IMAP_*      — m5 mailbox poller 收邮件
  NOTIFY_TO   — 审核员邮箱 (m3 APV / m4 通知 / m5 REJECT 都发到这里)

LLM 切换: 改 LLM_BASE_URL / LLM_API_KEY / LLM_MODEL 即可在 DeepSeek vs Anthropic
官方 OpenAI-兼容端点 之间切, 调用方代码不变 (统一走 openai SDK).

用法:
  from growthpress.core import get_settings
  s = get_settings()                       # 单例, lru_cache
  print(s.llm_provider, s.llm_base_url)
"""
from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """所有环境变量集中读 (.env + 系统 env). 字段名小写, 自动映射 ENV_VAR."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",   # 容忍 .env 里多余字段 (向前兼容)
    )

    # ===== LLM (m0) =====
    llm_provider: Literal["deepseek", "anthropic"] = "deepseek"
    llm_api_key: SecretStr = SecretStr("")
    llm_base_url: str = "https://api.deepseek.com"
    llm_model: str = "deepseek-chat"

    # ===== SMTP (m3/m4/m5 发邮件) =====
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_pass: SecretStr = SecretStr("")

    # ===== IMAP (m5 收邮件) =====
    imap_host: str = "imap.gmail.com"
    imap_port: int = 993
    imap_user: str = ""
    imap_pass: SecretStr = SecretStr("")
    imap_folder: str = "INBOX"

    # ===== 通知 =====
    notify_to: str = ""        # 审核员邮箱 (必填, 但 W1 阶段允许空便于跑骨架)


@lru_cache
def get_settings() -> Settings:
    """单例. 跨模块共享同一份配置, 避免每次重读 .env."""
    return Settings()
