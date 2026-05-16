"""Publisher 抽象: 各平台实现需符合的协议.

设计目标:
  - 一篇 Content 可分发多平台 (小红书 + 微博 + 公众号 同步发)
  - 一个平台支持多种内容类型 (小红书既能图文也能视频)
  - 新增平台 = 新开 repo + 实现 Publisher + 注册 entry_point, 主项目零改动

内容类型 (ContentType):
  image_note  小红书图文 / 微博图文
  video       抖音 / B站 / 小红书视频
  article     公众号 / 知乎专栏 (markdown 长文)
  short_text  微博短文 / Twitter

注册方式 (pyproject.toml):
  [project.entry-points."growthpress.platforms"]
  xiaohongshu = "xhs_publisher.publisher:XhsPublisher"   # 推荐: 指向 Publisher class
  # 或 = "xhs_publisher.publish:publish_image_note"      # 兼容: 指向老 callable

m4 publisher.agent 调用:
  publisher = await discover_platforms()["xiaohongshu"]
  result = await publisher.publish(content, dry_run=False)
"""
from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Any, NotRequired, Protocol, TypedDict, runtime_checkable


class ContentType(str, Enum):
    IMAGE_NOTE = "image_note"
    VIDEO = "video"
    ARTICLE = "article"
    SHORT_TEXT = "short_text"


class Content(TypedDict):
    """一篇待发布内容. m1 生成 → m2 审 → m3 批 → m4 按平台分发."""

    type: ContentType
    title: str                       # SHORT_TEXT 可为空字符串
    body: str                        # markdown 正文
    media: list[Path]                # IMAGE_NOTE: 图片列表; VIDEO: 通常 1 个视频文件
    tags: list[str]
    cover: NotRequired[Path]         # VIDEO 封面 (省略则平台取首帧)
    meta: NotRequired[dict[str, Any]]  # 平台特定字段 (B 站分区 / 抖音话题 / 微博位置)


class PublishResult(TypedDict):
    """publisher 返回结构. m4 汇总成发布通知邮件."""

    success: bool
    dry_run: bool
    platform: str                    # "xiaohongshu" / "douyin" / "weibo" ...
    url: str | None                  # 发布后最终 URL
    screenshot: str | None           # 截图绝对路径 (可选)
    error: str | None                # 失败时分类常量 (ERR_SESSION_EXPIRED 等)
    error_detail: str | None         # 人类可读错误描述
    elapsed_sec: float


@runtime_checkable
class Publisher(Protocol):
    """通用 publisher 协议. 各平台 entry_point 暴露 Publisher class 或实例.

    实现示例 (xhs-publisher 接入 m4 时):
        class XhsPublisher:
            name = "xiaohongshu"
            supported_types = [ContentType.IMAGE_NOTE, ContentType.VIDEO]

            async def publish(self, content, *, dry_run=False):
                # 复用 xhs_publisher.publish.publish_image_note
                ...
    """

    name: str
    supported_types: list[ContentType]

    async def publish(
        self,
        content: Content,
        *,
        dry_run: bool = False,
    ) -> PublishResult: ...
