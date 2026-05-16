"""CLI: python -m growthpress.reviewer <draft_id>

跑 m2 review 一次, 输出 Verdict + 改 drafts.state.

需要 draft 已经在 db (state=new). 若没有可先用 init_test_draft 辅助脚本.
"""
from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

from ..db import Database
from .agent import review

DB_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "runs.db"


async def _main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python -m growthpress.reviewer <draft_id>", file=sys.stderr)
        sys.exit(2)
    draft_id = sys.argv[1]

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(message)s",
    )

    async with Database.open(DB_PATH) as db:
        writer = asyncio.create_task(db.writer_loop())
        try:
            verdict = await review(draft_id, db)
            if verdict is None:
                print(f"[m2] draft {draft_id} 不存在或不在 new state", file=sys.stderr)
                sys.exit(1)
            print()
            print(f"=== Verdict for {draft_id} ===")
            print(f"  passed     : {verdict.passed}")
            print(f"  compliance : passed={verdict.compliance.passed} "
                  f"severity={verdict.compliance.severity}")
            for it in verdict.compliance.issues:
                print(f"               - {it}")
            if verdict.quality:
                print(f"  quality    : passed={verdict.quality.passed} "
                      f"score={verdict.quality.score}")
                for it in verdict.quality.issues:
                    print(f"               - {it}")
            if verdict.platform:
                print(f"  platform   : passed={verdict.platform.passed}")
                for plat, issues in verdict.platform.issues_by_platform.items():
                    if issues:
                        print(f"               - {plat}: {issues}")
            print(f"  all_issues : {len(verdict.all_issues)}")
        finally:
            writer.cancel()
            try:
                await writer
            except asyncio.CancelledError:
                pass


if __name__ == "__main__":
    asyncio.run(_main())
