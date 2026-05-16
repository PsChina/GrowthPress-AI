"""m0 core: 全局配置 (pydantic-settings 读 .env).

字段分组:
  LLM_*       — m1/m2 用的 LLM (走 anthropic SDK + Anthropic 协议)
                默认 DeepSeek 接管 (https://api.deepseek.com/anthropic, web_search 免费)
                可切真 Anthropic (https://api.anthropic.com, 按 token 计费)
  SMTP_*      — m3/m4/m5 发邮件
  IMAP_*      — m5 mailbox poller 收邮件
  NOTIFY_TO   — 审核员邮箱 (m3 APV / m4 通知 / m5 REJECT 都发到这里)

用法:
  from growthpress.core import get_settings
  s = get_settings()                       # 单例, lru_cache
  print(s.llm_base_url, s.llm_model)
"""
from __future__ import annotations

from functools import lru_cache

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """所有环境变量集中读 (.env + 系统 env). 字段名小写, 自动映射 ENV_VAR."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ===== LLM (m0) — anthropic SDK 走任一 Anthropic-兼容端点 =====
    llm_api_key: SecretStr = SecretStr("")
    llm_base_url: str = "https://api.deepseek.com/anthropic"
    llm_model: str = "deepseek-v4-pro[1m]"   # m1 默认 (单模型). 切真 Anthropic 改 claude-sonnet-4-5
    # m2+ 经 llm_router, 按 task → flash / pro 选模型
    llm_model_flash: str = "deepseek-v4-flash[1m]"   # 短/规则任务 (合规检测/分类/平台适配)
    llm_model_pro:   str = "deepseek-v4-pro[1m]"     # 创作/深推 (写作/质量审/改稿)

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
    notify_to: str = ""


@lru_cache
def get_settings() -> Settings:
    """单例. 跨模块共享同一份配置, 避免每次重读 .env."""
    return Settings()
