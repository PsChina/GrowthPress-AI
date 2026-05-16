"""Publisher agent (m4): 多平台并发发布 + 汇总通知.

平台发现: 通过 importlib.metadata.entry_points(group="growthpress.platforms").
- 内置开源平台 (juejin / csdn / zhihu) 在本仓 platforms/<name>.py, 自己 register
  to entry_point
- xiaohongshu 走独立私有仓 xhs-publisher, 装到 venv 后自动 discover

W1 阶段: 这里只是 stub + discover 接口. m4 完整实现在 W3 之前不重要.
"""
from __future__ import annotations

import logging
import sys
from importlib.metadata import entry_points
from typing import Callable

log = logging.getLogger("growthpress.publisher")

EP_GROUP = "growthpress.platforms"


def discover_platforms() -> dict[str, Callable]:
    """扫所有装在当前 venv 里的 growthpress.platforms entry_point.

    Returns:
        {platform_name: publish_callable} 例如 {"xiaohongshu": publish_image_note}
        无 platform 装时返回空 dict (m4 publisher 上层应优雅处理).
    """
    found: dict[str, Callable] = {}
    # Python 3.10+ 用 .select(group=...), 旧版 entry_points()[group]
    if sys.version_info >= (3, 10):
        eps = entry_points(group=EP_GROUP)
    else:  # pragma: no cover
        eps = entry_points().get(EP_GROUP, [])  # type: ignore[attr-defined]
    for ep in eps:
        try:
            found[ep.name] = ep.load()
            log.info(f"[platform] discovered: {ep.name} → {ep.value}")
        except Exception as e:
            log.warning(f"[platform] load failed: {ep.name} ({ep.value}): {e}")
    return found


def list_available_platforms() -> list[str]:
    """sanity / 诊断用. 不实际 load callable."""
    if sys.version_info >= (3, 10):
        return sorted(ep.name for ep in entry_points(group=EP_GROUP))
    return sorted(ep.name for ep in entry_points().get(EP_GROUP, []))  # type: ignore[attr-defined]
