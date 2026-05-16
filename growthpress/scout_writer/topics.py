"""m1 调度配置加载 + 选下一个 topic.

加载 config/topics.yaml (个人配置, .gitignore 排除), 后备 topics.example.yaml.
被 orchestrator.schedule_runs() 调用.

选 topic 策略 (W1):
  SQL 查每个 topic 最近一次 drafts.created_at, 取最久没跑过的 (None 优先).
  → 自动 round-robin, 不需要维护额外状态.

未来扩展 (m5 W3+):
  - LLM 把"路线" (宽泛主题) 派生为今天的具体 topic
  - 邮件 [ROUTE-ADD/REMOVE/CRON] 命令热改 topics 列表
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import yaml

from ..db import Database

log = logging.getLogger("growthpress.scout_writer.topics")

DEFAULT_PATH = Path("config/topics.yaml")
EXAMPLE_PATH = Path("config/topics.example.yaml")


@dataclass(frozen=True)
class ScheduleCfg:
    enabled: bool
    interval_minutes: int


@dataclass(frozen=True)
class TopicsConfig:
    schedule: ScheduleCfg
    topics: list[str]


def load_topics_config(path: Path | str = DEFAULT_PATH) -> TopicsConfig:
    """读 topics.yaml. 文件不存在则:
      - 在 example 存在时回退到 example (开发期方便)
      - 都不存在则返回空配置 (scheduler 自动 disabled)
    """
    p = Path(path)
    if not p.exists():
        if EXAMPLE_PATH.exists():
            log.warning(
                f"[topics] {p} 不存在, 回退到 {EXAMPLE_PATH} "
                f"(生产请 cp 一份为 topics.yaml 改成自己的)"
            )
            p = EXAMPLE_PATH
        else:
            log.warning(f"[topics] 都不存在 ({p} / {EXAMPLE_PATH}), scheduler 将 disabled")
            return TopicsConfig(
                schedule=ScheduleCfg(enabled=False, interval_minutes=360),
                topics=[],
            )

    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    if isinstance(data, list):
        raise ValueError(
            f"{p} 是 W0 旧 schema (一级 list of mappings). 当前格式: "
            f"mapping {{schedule: {{enabled, interval_minutes}}, topics: [...]}}. "
            f"迁移: mv {p} {p}.old, cp {EXAMPLE_PATH} {p}, 然后改"
        )
    sched = data.get("schedule") or {}
    return TopicsConfig(
        schedule=ScheduleCfg(
            enabled=bool(sched.get("enabled", False)),
            interval_minutes=int(sched.get("interval_minutes", 360)),
        ),
        topics=list(data.get("topics") or []),
    )


async def pick_next_topic(topics: list[str], db: Database) -> str | None:
    """选最久没跑过的 topic.

    SQL 查每个 topic 最近 created_at, 取最老 (None 优先 — 从未跑过的先跑).
    """
    if not topics:
        return None
    placeholders = ",".join("?" * len(topics))
    rows = await db.read(
        f"SELECT topic, MAX(created_at) AS last_at FROM drafts "
        f"WHERE topic IN ({placeholders}) GROUP BY topic",
        tuple(topics),
    )
    last_at = {r["topic"]: r["last_at"] for r in rows}
    # None < 任何字符串 (key 用 (has_run_flag, last_at) 排序: 没跑过的优先)
    return min(topics, key=lambda t: (1 if last_at.get(t) else 0, last_at.get(t) or ""))
