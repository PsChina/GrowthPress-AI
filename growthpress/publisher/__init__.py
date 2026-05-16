"""m4 publisher — 多平台分发.

公开 API:
  ContentType        — 枚举: image_note / video / article / short_text
  Content            — TypedDict: 一篇待发布内容 (title / body / media / tags / ...)
  PublishResult      — TypedDict: publisher 返回结构
  Publisher          — Protocol: 各平台需实现的接口
  discover_platforms — 扫 entry_points 拿 {name: Publisher}
  platforms_for      — 按内容类型筛 publisher
"""
from .base import Content, ContentType, Publisher, PublishResult
from .agent import discover_platforms, list_available_platforms, platforms_for

__all__ = [
    "Content",
    "ContentType",
    "Publisher",
    "PublishResult",
    "discover_platforms",
    "list_available_platforms",
    "platforms_for",
]
