"""m1 输出 schema. pydantic 校验 LLM 返回的 JSON 元数据 block."""
from __future__ import annotations

from pydantic import BaseModel, Field


class Source(BaseModel):
    title: str
    url: str   # 不用 HttpUrl: DeepSeek 偶尔返回带 utm 参数 / 跳转链接, HttpUrl 校验过严


class DraftSchema(BaseModel):
    title: str = Field(..., max_length=60, description="标题, 中文 ≤30 字")
    tags: list[str] = Field(..., min_length=1, max_length=10)
    body_md: str = Field(..., min_length=200, description="markdown 正文 800-1500 字")
    summary: str = Field(..., max_length=300, description="摘要 ≤100 字")
    sources: list[Source] = Field(default_factory=list)
