"""m1 scout_writer — DeepSeek + web_search 调研, 生成结构化 draft.

公开 API:
    run(topic, db=None, drafts_dir="data/drafts") -> (draft_id, DraftSchema)
    DraftSchema  — pydantic 输出 schema
    Source       — 来源条目
"""
from .agent import run
from .schemas import DraftSchema, Source

__all__ = ["run", "DraftSchema", "Source"]
