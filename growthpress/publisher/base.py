"""Publisher 抽象: 各平台实现需符合的协议.

约定: 每个平台暴露一个 publish_image_note (或类似命名) 函数, 通过 entry_point
注册到 group "growthpress.platforms". 函数签名:

  def publish_image_note(
      *,
      title: str,
      body: str,
      image_paths: list[Path],
      dry_run: bool = False,
      **kwargs,                # 平台特定参数, 不识别忽略
  ) -> dict:                   # 返回 result dict (见下面 PublishResult schema)

返回 dict 字段 (PublishResult schema):
  success: bool
  dry_run: bool
  url: str | None              # 发布后的最终 URL
  screenshot: str | None       # 截图绝对路径 (可选)
  error: str | None            # 失败时的错误分类 (平台特定常量)
  error_detail: str | None     # 人类可读
  elapsed_sec: float
  (其他字段平台可加, m4 publisher agent 不强求结构)

注意: 函数应该是同步的 (因 xiaohongshu Playwright 用 sync_playwright);
m4 publisher.agent 用 asyncio.to_thread 包成 async 调用.
"""
from __future__ import annotations

from pathlib import Path
from typing import Protocol, Any


class ImagePublisher(Protocol):
    """图文笔记发布器协议. 仅用作类型提示, 不强制继承."""

    def __call__(
        self,
        *,
        title: str,
        body: str,
        image_paths: list[Path],
        dry_run: bool = False,
        **kwargs: Any,
    ) -> dict: ...
