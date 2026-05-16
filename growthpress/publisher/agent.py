"""Publisher agent (m4): 多平台并发发布 + 汇总通知.

平台发现: importlib.metadata.entry_points(group="growthpress.platforms").
- 内置开源平台 (juejin / csdn / zhihu) 在本仓 platforms/<name>.py 注册 entry_point
- xiaohongshu / douyin / weibo / bili 走独立 (可能私有) 仓, 装到 venv 后自动 discover

兼容三种 entry_point 输出 (优先级从高到低):
  1. Publisher 实例   (直接用)
  2. Publisher class  (无参实例化)
  3. 老 callable      (function-style, 自动包装成 LegacyImagePublisher)

W1 阶段: stub + discover. m4 完整实现在 W3 之前不重要.
"""
from __future__ import annotations

import asyncio
import inspect
import logging
import sys
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any, Callable

from .base import Content, ContentType, Publisher, PublishResult

log = logging.getLogger("growthpress.publisher")

EP_GROUP = "growthpress.platforms"


class LegacyImagePublisher:
    """包装老 callable (publish_image_note function) → Publisher 协议.

    兼容 xhs-publisher v0.2 现有 entry_point: 指向 publish_image_note function.
    后续 xhs-publisher 升级出 XhsPublisher class 后, 此 adapter 自动让位.
    """

    def __init__(self, name: str, fn: Callable[..., dict]) -> None:
        self.name = name
        self._fn = fn
        self.supported_types: list[ContentType] = [ContentType.IMAGE_NOTE]

    async def publish(
        self,
        content: Content,
        *,
        dry_run: bool = False,
    ) -> PublishResult:
        if content["type"] != ContentType.IMAGE_NOTE:
            raise ValueError(
                f"legacy adapter {self.name!r} only supports image_note, "
                f"got {content['type']}"
            )
        # 老 function 是 sync (Playwright sync_playwright), 用 to_thread 包成 async
        result: dict = await asyncio.to_thread(
            self._fn,
            title=content["title"],
            body=content["body"],
            image_paths=content["media"],
            dry_run=dry_run,
        )
        return {
            "success": bool(result.get("success", False)),
            "dry_run": bool(result.get("dry_run", dry_run)),
            "platform": self.name,
            "url": result.get("url"),
            "screenshot": result.get("screenshot"),
            "error": result.get("error"),
            "error_detail": result.get("error_detail"),
            "elapsed_sec": float(result.get("elapsed_sec", 0)),
        }


def _adapt(name: str, loaded: Any) -> Publisher:
    """把 entry_point 加载结果转成 Publisher.

    判断顺序 (isclass 必须先 — Protocol runtime_checkable 对 class 也算 instance,
    会跳过实例化导致 method 调用 missing self):
      1. 是 class    → 实例化, 检查实例符合 Publisher
      2. 是 Publisher 实例 (已实例化或单例) → 直接用
      3. 是 function → 包装 LegacyImagePublisher (兼容老 entry_point)
    """
    if inspect.isclass(loaded):
        instance = loaded()
        if not isinstance(instance, Publisher):
            raise TypeError(
                f"{name!r}: class {loaded.__name__} 实例化后不符合 Publisher 协议 "
                f"(缺 name / supported_types / async publish)"
            )
        return instance
    if isinstance(loaded, Publisher):
        return loaded
    if callable(loaded):
        log.warning(
            f"[platform] {name!r}: 老 function-style entry_point, "
            f"自动包装成 LegacyImagePublisher (仅支持 image_note)"
        )
        return LegacyImagePublisher(name=name, fn=loaded)
    raise TypeError(
        f"{name!r}: entry_point 加载结果既不是 Publisher 也不是 class/callable: {loaded!r}"
    )


def discover_platforms() -> dict[str, Publisher]:
    """扫所有装在当前 venv 里的 growthpress.platforms entry_point.

    Returns:
        {platform_name: Publisher} 例如 {"xiaohongshu": XhsPublisher(), "douyin": ...}
        无 platform 装时返回空 dict (m4 publisher 上层应优雅处理).
    """
    found: dict[str, Publisher] = {}
    if sys.version_info >= (3, 10):
        eps = entry_points(group=EP_GROUP)
    else:  # pragma: no cover
        eps = entry_points().get(EP_GROUP, [])  # type: ignore[attr-defined]
    for ep in eps:
        try:
            loaded = ep.load()
            found[ep.name] = _adapt(ep.name, loaded)
            log.info(
                f"[platform] discovered: {ep.name} → {ep.value} "
                f"(types={[t.value for t in found[ep.name].supported_types]})"
            )
        except Exception as e:
            log.warning(f"[platform] load failed: {ep.name} ({ep.value}): {e}")
    return found


def list_available_platforms() -> list[str]:
    """sanity / 诊断用. 不实际 load callable."""
    if sys.version_info >= (3, 10):
        return sorted(ep.name for ep in entry_points(group=EP_GROUP))
    return sorted(ep.name for ep in entry_points().get(EP_GROUP, []))  # type: ignore[attr-defined]


def platforms_for(content_type: ContentType, available: dict[str, Publisher]) -> list[Publisher]:
    """筛出能发某种内容类型的 publisher 列表 (m4 调度用).

    用法:
        all_pub = discover_platforms()
        video_targets = platforms_for(ContentType.VIDEO, all_pub)
        # → [DouyinPublisher, BiliPublisher, XhsPublisher] (按装的为准)
    """
    return [p for p in available.values() if content_type in p.supported_types]
