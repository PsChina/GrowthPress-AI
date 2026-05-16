"""m5 主题路线管理 handler — ROUTE-ADD / REMOVE / LIST / DEFAULT / CRON.

V1 实装 ADD / REMOVE / LIST (最常用). DEFAULT / CRON 留 stub.

操作 config/topics.yaml:
  - 改前备份成 topics.yaml.bak (覆盖式, 只保留最近一次, 防误改回滚用)
  - yaml.safe_dump 写回 (用 allow_unicode=True 保中文不被转义)
  - 文件不存在 → 用 example 创建 (load_topics_config 同款 fallback)

回信 (subject `Re: [ROUTE-ADD-OK]` 等) 通过 aiosmtplib. 实际 SMTP 调用失败不挂
handler — log warning 即可, 邮件命令本身已经写入了 yaml.

注意: 当前 config/topics.yaml 可能是 W0 旧 list 格式 (workspace 实际配置).
load_topics_config() 对旧格式会抛 ValueError. ROUTE-* 命令统一按"新 mapping
格式"操作; 遇到旧格式 → 不破坏文件, 回信报错引导用户迁移.
"""
from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Any

import yaml

from ...core import get_settings
from ...db import Database
from ..schemas import InboundMsg
from ..subject_parser import SubjectKind, parse_subject

# 主题路线 yaml 路径 (旧 scout_writer.topics 模块已删, 这里内联).
# 新架构下 yaml 不再驱动 daemon, 只是 Claude 工作流可选读的选题候选清单.
_REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_PATH = _REPO_ROOT / "config" / "topics.yaml"
EXAMPLE_PATH = _REPO_ROOT / "config" / "topics.example.yaml"

log = logging.getLogger("growthpress.mailbox.route_command")


# ---------- yaml 读 / 写 (新 mapping 格式) ----------

def _load_yaml() -> dict[str, Any] | None:
    """读 config/topics.yaml, 返回新 mapping 格式 dict. 异常返回 None.

    None 表示: 文件是旧 list 格式 / 不是合法 yaml / 不是 mapping. 这种情况下
    ROUTE 命令应该回信报错, 不要自动 overwrite (会破坏用户配置).
    """
    path = DEFAULT_PATH
    if not path.exists():
        if EXAMPLE_PATH.exists():
            log.info(f"[route] {path} 不存在, 从 {EXAMPLE_PATH} 拷贝创建")
            shutil.copyfile(EXAMPLE_PATH, path)
        else:
            # 创建一个最小新格式
            path.write_text(
                "schedule:\n"
                "  enabled: false\n"
                "  interval_minutes: 360\n"
                "topics: []\n",
                encoding="utf-8",
            )

    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        log.error(f"[route] {path} yaml parse 失败: {e}")
        return None
    if not isinstance(data, dict):
        log.warning(
            f"[route] {path} 不是 mapping (可能是 W0 旧 list 格式), "
            f"ROUTE-* 命令暂不支持自动迁移"
        )
        return None
    # 保证 topics / schedule 字段存在
    data.setdefault("topics", [])
    data.setdefault("schedule", {"enabled": False, "interval_minutes": 360})
    return data


def _save_yaml(data: dict[str, Any]) -> None:
    """备份 + safe_dump 写回. 备份固定一份 (覆盖)."""
    path = DEFAULT_PATH
    if path.exists():
        backup = path.with_suffix(path.suffix + ".bak")
        shutil.copyfile(path, backup)
        log.debug(f"[route] 备份: {path} → {backup}")
    text = yaml.safe_dump(
        data,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
    )
    path.write_text(text, encoding="utf-8")


# ---------- 回信 (aiosmtplib) ----------

async def _send_reply(msg: InboundMsg, subject: str, body: str) -> None:
    """通过 aiosmtplib 给 sender 回信. 失败只 log, 不抛 (yaml 已写, 回信只是反馈).

    sender 字段是 RFC822 格式 (`Name <addr@x.com>`), aiosmtplib 接受这种格式.
    """
    s = get_settings()
    if not s.smtp_user or not s.smtp_pass.get_secret_value():
        log.warning(
            f"[route] SMTP 未配置 (smtp_user / smtp_pass 空), 跳过回信. "
            f"subject={subject!r}"
        )
        return

    try:
        # 延迟 import 避免在没用到 SMTP 时也加载
        import aiosmtplib
        from email.message import EmailMessage
    except ImportError as e:
        log.error(f"[route] aiosmtplib 未安装: {e}")
        return

    email = EmailMessage()
    email["From"] = s.smtp_user
    email["To"] = msg.sender or s.notify_to or s.smtp_user
    email["Subject"] = subject
    if msg.message_id:
        email["In-Reply-To"] = msg.message_id
        email["References"] = msg.message_id
    email.set_content(body)

    try:
        await aiosmtplib.send(
            email,
            hostname=s.smtp_host,
            port=s.smtp_port,
            username=s.smtp_user,
            password=s.smtp_pass.get_secret_value(),
            start_tls=True,
            timeout=30,
        )
        log.info(f"[route] 回信 ok: to={email['To']!r} subject={subject!r}")
    except Exception as e:
        log.warning(f"[route] 回信失败 (yaml 已写, 仅反馈丢失): {e!r}")


# ---------- 各子命令 ----------

async def _handle_add(msg: InboundMsg) -> None:
    """[ROUTE-ADD] 正文一行 (≤80 字) → topics list append."""
    line = (msg.body_text or "").strip().split("\n", 1)[0].strip()
    if not line:
        await _send_reply(
            msg,
            subject="Re: [ROUTE-ADD-ERR] 正文为空",
            body="ROUTE-ADD 需要正文写一行新的主题路线 (≤80 字).\n例: AI 编程工具横向对比",
        )
        return
    if len(line) > 80:
        await _send_reply(
            msg,
            subject="Re: [ROUTE-ADD-ERR] 主题过长",
            body=f"主题 ≤80 字. 当前 {len(line)} 字:\n{line}",
        )
        return

    data = _load_yaml()
    if data is None:
        await _send_reply(
            msg,
            subject="Re: [ROUTE-ADD-ERR] yaml 格式不支持",
            body=(
                "config/topics.yaml 当前不是新 mapping 格式 (可能是 W0 旧 list of "
                "mappings). ROUTE-* 命令暂不能自动迁移. 请先把 yaml 改成:\n\n"
                "schedule:\n  enabled: false\n  interval_minutes: 360\ntopics:\n"
                "  - \"路线 1\"\n  - \"路线 2\"\n"
            ),
        )
        return

    topics: list[str] = list(data.get("topics") or [])
    if line in topics:
        await _send_reply(
            msg,
            subject="Re: [ROUTE-ADD-DUP] 已存在",
            body=f"路线已在 topics 列表里, 不重复添加:\n{line}",
        )
        return

    topics.append(line)
    data["topics"] = topics
    _save_yaml(data)
    log.info(f"[m5/route-add] 新增路线: {line!r}, 当前共 {len(topics)} 条")
    await _send_reply(
        msg,
        subject="Re: [ROUTE-ADD-OK] 路线已添加",
        body=f"已添加路线:\n  {line}\n\n当前 topics 共 {len(topics)} 条.",
    )


async def _handle_remove(msg: InboundMsg) -> None:
    """[ROUTE-REMOVE] 正文 = 精确字符串或前缀匹配."""
    line = (msg.body_text or "").strip().split("\n", 1)[0].strip()
    if not line:
        await _send_reply(
            msg,
            subject="Re: [ROUTE-REMOVE-ERR] 正文为空",
            body="ROUTE-REMOVE 需要正文写要删除的主题 (精确字符串或前缀).",
        )
        return

    data = _load_yaml()
    if data is None:
        await _send_reply(
            msg,
            subject="Re: [ROUTE-REMOVE-ERR] yaml 格式不支持",
            body="config/topics.yaml 不是新 mapping 格式, 请手动迁移后再用 ROUTE 命令.",
        )
        return

    topics: list[str] = list(data.get("topics") or [])
    # 精确匹配优先, 没命中再用前缀
    removed: list[str] = []
    if line in topics:
        topics.remove(line)
        removed.append(line)
    else:
        # 前缀匹配 (大小写不敏感)
        line_lower = line.lower()
        kept: list[str] = []
        for t in topics:
            if t.lower().startswith(line_lower):
                removed.append(t)
            else:
                kept.append(t)
        topics = kept

    if not removed:
        await _send_reply(
            msg,
            subject="Re: [ROUTE-REMOVE-MISS] 未匹配",
            body=(
                f"未找到匹配的路线 (精确或前缀): {line!r}\n"
                f"当前 topics:\n" + "\n".join(f"  - {t}" for t in topics)
            ),
        )
        return

    data["topics"] = topics
    _save_yaml(data)
    log.info(f"[m5/route-remove] 删除 {len(removed)} 条: {removed}, 剩 {len(topics)} 条")
    await _send_reply(
        msg,
        subject="Re: [ROUTE-REMOVE-OK] 路线已删除",
        body=(
            f"已删除 {len(removed)} 条路线:\n"
            + "\n".join(f"  - {t}" for t in removed)
            + f"\n\n当前 topics 剩 {len(topics)} 条."
        ),
    )


async def _handle_list(msg: InboundMsg) -> None:
    """[ROUTE-LIST] 正文空 → 回信列当前 schedule + topics."""
    data = _load_yaml()
    if data is None:
        await _send_reply(
            msg,
            subject="Re: [ROUTE-LIST-ERR] yaml 格式不支持",
            body=(
                "config/topics.yaml 不是新 mapping 格式 (可能是 W0 旧 list of "
                "mappings). 当前文件原样保留, 未变动.\n"
                "请改成新格式后再用 ROUTE-LIST."
            ),
        )
        return

    sched = data.get("schedule") or {}
    topics: list[str] = list(data.get("topics") or [])
    lines = [
        "GrowthPress AI — 当前主题路线",
        "",
        f"schedule:",
        f"  enabled: {sched.get('enabled', False)}",
        f"  interval_minutes: {sched.get('interval_minutes', 360)}",
        "",
        f"topics ({len(topics)} 条):",
    ]
    if not topics:
        lines.append("  (空)")
    else:
        for i, t in enumerate(topics, 1):
            lines.append(f"  {i}. {t}")

    await _send_reply(
        msg,
        subject="Re: [ROUTE-LIST] 当前路线清单",
        body="\n".join(lines),
    )


async def _handle_default_or_cron(msg: InboundMsg, kind: SubjectKind) -> None:
    """V1 stub: [ROUTE-DEFAULT] / [ROUTE-CRON] 留待 W3+ 实施."""
    name = "ROUTE-DEFAULT" if kind == SubjectKind.ROUTE_DEFAULT else "ROUTE-CRON"
    log.info(f"[m5/route] {name} 收到, V1 未实施 (TODO W3+): subject={msg.subject!r}")
    await _send_reply(
        msg,
        subject=f"Re: [{name}-TODO] V1 未实施",
        body=(
            f"{name} 命令将在 W3+ 实施. 当前 V1 仅支持:\n"
            "  [ROUTE-ADD] / [ROUTE-REMOVE] / [ROUTE-LIST]\n"
        ),
    )


# ---------- 入口 ----------

async def handle_route_command(msg: InboundMsg, db: Database) -> None:
    """ROUTE-* 命令统一入口, 按 subject_parser 识别再分派.

    db 入参留着接口一致性 — V1 不写 db (路线全部存 yaml). W3+ 如果要把路线
    也入 db (跨设备同步 / CLI 兜底), 这里加 db 写入.
    """
    info = parse_subject(msg.subject)
    if info.kind == SubjectKind.ROUTE_ADD:
        await _handle_add(msg)
    elif info.kind == SubjectKind.ROUTE_REMOVE:
        await _handle_remove(msg)
    elif info.kind == SubjectKind.ROUTE_LIST:
        await _handle_list(msg)
    elif info.kind in (SubjectKind.ROUTE_DEFAULT, SubjectKind.ROUTE_CRON):
        await _handle_default_or_cron(msg, info.kind)
    else:
        log.warning(
            f"[m5/route] 非 ROUTE-* subject 被路由进来 (dispatcher bug?): "
            f"subject={msg.subject!r}"
        )
