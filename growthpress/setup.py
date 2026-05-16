"""GrowthPress AI 首次配置向导 — 交互填 .env + 顺手 cp topics.yaml.

用法:
    uv run growthpress-setup
    或: uv run python -m growthpress.setup

按 [1/4] LLM / [2/4] SMTP / [3/4] IMAP / [4/4] 通知收件人 顺序问.
默认值显示在 [括号] 里, 回车接受默认. 密码 / API key 用 getpass 不在终端显示.

已有 .env 时: 显示字段数 + 询问是否覆盖. 覆盖时旧值作为默认值 (回车保留).

不动其他文件 / 不装依赖 / 不跑 daemon. 完成后写 .env (chmod 600) + 可选
cp config/topics.example.yaml → config/topics.yaml.
"""
from __future__ import annotations

import getpass
import shutil
import stat
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT / ".env"
ENV_EXAMPLE = ROOT / ".env.example"
TOPICS_PATH = ROOT / "config" / "topics.yaml"
TOPICS_EXAMPLE = ROOT / "config" / "topics.example.yaml"


def _ask(prompt: str, default: str | None = None, *, secret: bool = False,
         allow_empty: bool = False) -> str:
    """读一个字段. 回车用 default. 必填项空了重问 (除非 allow_empty)."""
    if secret:
        hint = " (隐藏)" if not default else " (隐藏, 回车保留旧值)"
    elif default:
        hint = f" [{default}]"
    else:
        hint = ""

    while True:
        if secret:
            val = getpass.getpass(f"  {prompt}{hint}: ")
        else:
            val = input(f"  {prompt}{hint}: ").strip()
        if val:
            return val
        if default is not None:
            return default
        if allow_empty:
            return ""
        print("    (必填, 重新输入)")


def _ask_yn(prompt: str, default: bool = True) -> bool:
    suffix = " [Y/n]" if default else " [y/N]"
    while True:
        val = input(f"  {prompt}{suffix}: ").strip().lower()
        if not val:
            return default
        if val in ("y", "yes", "是", "好"):
            return True
        if val in ("n", "no", "否", "不"):
            return False


def _parse_env(path: Path) -> dict[str, str]:
    """轻量 .env parser (足够这里用)."""
    if not path.exists():
        return {}
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, _, v = s.partition("=")
        # 处理 inline 注释: VAR=value  # comment
        v = v.split("#", 1)[0].strip().strip("'\"")
        out[k.strip()] = v
    return out


def _write_env(values: dict[str, str], path: Path) -> None:
    """按 .env.example 模板顺序写 .env, 保留注释. example 缺的 key 追加到末尾."""
    if ENV_EXAMPLE.exists():
        template = ENV_EXAMPLE.read_text(encoding="utf-8").splitlines()
    else:
        template = []

    new_lines: list[str] = []
    written: set[str] = set()
    for raw in template:
        stripped = raw.strip()
        if "=" in stripped and not stripped.startswith("#"):
            k, _, rest = stripped.partition("=")
            k = k.strip()
            if k in values:
                # 保留行内注释 (例如 SMTP_PASS=  # Gmail App Password)
                if "#" in rest:
                    eq_idx = raw.find("=")
                    comment_idx = raw.find("#", eq_idx)
                    comment = "  " + raw[comment_idx:] if comment_idx > 0 else ""
                else:
                    comment = ""
                new_lines.append(f"{k}={values[k]}{comment}")
                written.add(k)
                continue
        new_lines.append(raw)

    # 模板里没的 key 追加 (e.g. LLM_MODEL_FLASH/PRO 是后加字段)
    extras = [k for k in values if k not in written]
    if extras:
        new_lines.append("")
        new_lines.append("# 向导新增字段")
        for k in extras:
            new_lines.append(f"{k}={values[k]}")

    path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)   # 600


def main() -> None:
    print()
    print("===========================================")
    print(" GrowthPress AI 配置向导")
    print("===========================================")
    print(f"  → 将写入 {ENV_PATH}")
    print()

    existing = _parse_env(ENV_PATH) if ENV_PATH.exists() else {}
    if existing:
        print(f"  ⚠ {ENV_PATH.name} 已存在, 含 {len(existing)} 个字段.")
        print(f"    继续会覆盖, 但每个字段会先显示旧值作默认 (回车保留).")
        if not _ask_yn("继续?", default=True):
            print("\n已取消.")
            sys.exit(0)
        print()

    # ---------- [1/4] LLM ----------
    print("[1/4] LLM (DeepSeek 接管 Anthropic 协议, web_search server tool 免费)")
    llm_key = _ask("LLM_API_KEY (DeepSeek API key, sk-...)",
                   default=existing.get("LLM_API_KEY") or None, secret=True)
    llm_url = _ask("LLM_BASE_URL",
                   default=existing.get("LLM_BASE_URL", "https://api.deepseek.com/anthropic"))
    llm_model = _ask("LLM_MODEL (m1 默认)",
                     default=existing.get("LLM_MODEL", "deepseek-v4-pro[1m]"))
    llm_flash = _ask("LLM_MODEL_FLASH (m2+ 轻任务)",
                     default=existing.get("LLM_MODEL_FLASH", "deepseek-v4-flash[1m]"))
    llm_pro = _ask("LLM_MODEL_PRO (m2+ 创作/深推)",
                   default=existing.get("LLM_MODEL_PRO", "deepseek-v4-pro[1m]"))
    print()

    # ---------- [2/4] SMTP ----------
    print("[2/4] SMTP (m3 发 APV / m4 发 PUB / 日报)")
    smtp_host = _ask("SMTP_HOST", default=existing.get("SMTP_HOST", "smtp.gmail.com"))
    smtp_port = _ask("SMTP_PORT", default=existing.get("SMTP_PORT", "587"))
    smtp_user = _ask("SMTP_USER (you@gmail.com)", default=existing.get("SMTP_USER") or None)
    smtp_pass = _ask("SMTP_PASS (Gmail 用 App Password, 不是登录密码)",
                     default=existing.get("SMTP_PASS") or None, secret=True)
    print()

    # ---------- [3/4] IMAP ----------
    print("[3/4] IMAP (m5 收回信)")
    print("  通常跟 SMTP 同账号, 直接回车用默认即可.")
    imap_host = _ask("IMAP_HOST", default=existing.get("IMAP_HOST", "imap.gmail.com"))
    imap_port = _ask("IMAP_PORT", default=existing.get("IMAP_PORT", "993"))
    imap_user = _ask("IMAP_USER", default=existing.get("IMAP_USER") or smtp_user)
    imap_pass = _ask("IMAP_PASS",
                     default=existing.get("IMAP_PASS") or smtp_pass, secret=True)
    imap_folder = _ask("IMAP_FOLDER", default=existing.get("IMAP_FOLDER", "INBOX"))
    print()

    # ---------- [4/4] 通知 ----------
    print("[4/4] 通知收件人 (APV / REJECT / PUB / 日报 都发到这里)")
    notify_to = _ask("NOTIFY_TO", default=existing.get("NOTIFY_TO") or smtp_user)
    print()

    values: dict[str, str] = {
        "LLM_API_KEY": llm_key,
        "LLM_BASE_URL": llm_url,
        "LLM_MODEL": llm_model,
        "LLM_MODEL_FLASH": llm_flash,
        "LLM_MODEL_PRO": llm_pro,
        "SMTP_HOST": smtp_host,
        "SMTP_PORT": smtp_port,
        "SMTP_USER": smtp_user,
        "SMTP_PASS": smtp_pass,
        "IMAP_HOST": imap_host,
        "IMAP_PORT": imap_port,
        "IMAP_USER": imap_user,
        "IMAP_PASS": imap_pass,
        "IMAP_FOLDER": imap_folder,
        "NOTIFY_TO": notify_to,
    }

    _write_env(values, ENV_PATH)
    print(f"✓ {ENV_PATH.name} 已写入 (chmod 600)")

    # 顺手处理 topics.yaml
    if not TOPICS_PATH.exists() and TOPICS_EXAMPLE.exists():
        print()
        if _ask_yn(f"从 example 创建 {TOPICS_PATH.relative_to(ROOT)}?",
                   default=True):
            shutil.copy(TOPICS_EXAMPLE, TOPICS_PATH)
            print(f"✓ {TOPICS_PATH.relative_to(ROOT)} 已创建.")
            print("  改 schedule.enabled=true + 调整 topics 列表后, daemon 自动调度.")

    print()
    print("===========================================")
    print(" 完成!")
    print("===========================================")
    print()
    print("下一步:")
    print("  uv run growthpress                                         # 启 daemon")
    print("  uv run python -m growthpress.scout_writer '<topic>'        # 单跑 m1")
    print("  uv run python scripts/e2e_test.py '<topic>'                # 端到端测")
    print()


if __name__ == "__main__":
    main()
