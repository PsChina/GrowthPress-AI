"""CLI 测试入口: python -m growthpress.scout_writer "<topic>"

不挂 db, 仅落盘 markdown. 验证 m1 链路通后再挂 orchestrator.
"""
from __future__ import annotations

import asyncio
import logging
import sys

from .agent import run


async def _main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python -m growthpress.scout_writer '<topic>'", file=sys.stderr)
        sys.exit(2)
    topic = sys.argv[1]

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(message)s",
    )

    # tty 模式缺 LLM_API_KEY 时询问跑 setup wizard
    from ..preflight import ensure, maybe_interactive
    maybe_interactive(source="m1")
    ensure("llm_api_key", source="m1")

    draft_id, draft = await run(topic)
    print()
    print(f"=== draft {draft_id} ===")
    print(f"Title  : {draft.title}")
    print(f"Tags   : {', '.join(draft.tags)}")
    print(f"Body   : {len(draft.body_md)} chars")
    print(f"Summary: {draft.summary}")
    print(f"Sources: {len(draft.sources)}")
    for i, s in enumerate(draft.sources, 1):
        print(f"  [{i}] {s.title} - {s.url}")
    print(f"Saved  : data/drafts/{draft_id}.md")


if __name__ == "__main__":
    asyncio.run(_main())
