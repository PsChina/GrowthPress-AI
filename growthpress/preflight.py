"""启动 / 接任务前检测 .env 配置完整性, 缺字段时提示跑 setup wizard.

daemon cli_main + 各模块 CLI (m1/m2/e2e) 起始 call preflight.

tty 模式 (人工执行): 缺关键字段时询问"现在跑 growthpress-setup ?"
非 tty (launchd 后台): 只 log warning, 不阻塞, 各 task 已有自己的 graceful exit
"""
from __future__ import annotations

import logging
import sys
from typing import Iterable

from .core.settings import get_settings

log = logging.getLogger("growthpress.preflight")

# 缺这些 LLM 完全跑不了 (m1 / m2 都依赖)
CRITICAL: tuple[str, ...] = ("llm_api_key",)

# 缺这些只影响对应模块 (graceful exit), 但功能不完整
RECOMMENDED: dict[str, str] = {
    "smtp_user":  "m3 APV / m4 PUB / 日报 邮件无法发出",
    "smtp_pass":  "同上 (Gmail 用 App Password)",
    "imap_user":  "m5 mailbox 收回信无法工作 (审批/撤销 等单向通知仍可)",
    "imap_pass":  "同上",
    "notify_to":  "审核员邮箱 — 各类通知都发到这里",
}


def _empty(value) -> bool:
    """SecretStr / str 统一判空."""
    if hasattr(value, "get_secret_value"):
        value = value.get_secret_value()
    return not value


def check() -> tuple[list[str], list[str]]:
    """返 (missing_critical, missing_recommended). 都是字段名 list."""
    s = get_settings()
    missing_critical = [k for k in CRITICAL if _empty(getattr(s, k, ""))]
    missing_recommended = [k for k in RECOMMENDED if _empty(getattr(s, k, ""))]
    return missing_critical, missing_recommended


def warn(*, source: str = "preflight") -> bool:
    """log 缺失字段. 返 True 表示关键字段都齐 (可正常跑), False 表示缺关键."""
    critical, recommended = check()
    if not critical and not recommended:
        return True
    if critical:
        log.error(
            f"[{source}] ⚠ 缺关键字段: {critical} — LLM 调用会失败. "
            f"运行 'uv run growthpress-setup' 配置"
        )
    if recommended:
        impact = ", ".join(f"{k}({RECOMMENDED[k]})" for k in recommended)
        log.warning(
            f"[{source}] 建议字段未配置: [{impact}]. 对应 task 会 graceful exit"
        )
    return not critical


def maybe_interactive(*, source: str = "preflight") -> None:
    """tty 模式 + 缺关键字段时询问"现在跑 setup?". 非 tty 跳过 (launchd 用).

    入口顺序:
      1. check() 看缺什么
      2. 非 tty / 无关键缺失 → return (warn 已 log)
      3. tty + 缺关键 → 询问. yes 就跑 setup.main(); no 就继续 (用户知情)
    """
    critical, _ = check()
    if not critical:
        return
    if not sys.stdin.isatty():
        return  # daemon / launchd 后台跑, 没 tty 直接跳过

    print()
    print(f"⚠ [{source}] 缺关键配置: {critical}")
    print("  不配置 LLM 调用会失败 (m1 调研无法跑).")
    try:
        val = input("  现在跑 growthpress-setup 交互向导? [Y/n]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return
    if val in ("", "y", "yes", "是", "好"):
        from .setup import main as setup_main
        setup_main()
        # 重置 lru_cache 让新值生效
        get_settings.cache_clear()
    else:
        print("  跳过. 你可以稍后运行 'uv run growthpress-setup'.")


def ensure(*fields: str, source: str = "preflight") -> None:
    """硬性要求某些字段必须配 — 缺则 raise SystemExit 1.

    用法 (单模块 CLI 入口, 没配置直接退出比模糊报错好):
        from ..preflight import ensure
        ensure("llm_api_key", source="m1")    # m1 没 LLM key 直接 exit
    """
    s = get_settings()
    missing = [f for f in fields if _empty(getattr(s, f, ""))]
    if not missing:
        return
    log.error(
        f"[{source}] 缺必填字段: {missing}. "
        f"运行 'uv run growthpress-setup' 配置后重试."
    )
    sys.exit(1)
