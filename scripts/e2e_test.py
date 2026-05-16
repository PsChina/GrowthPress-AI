"""End-to-end 集成测试: 投一个 topic 走完 m1 → m2 → m3 → m4 流水线.

用法:
    uv run python scripts/e2e_test.py "<topic>"

要求:
    - .env 已配 LLM_API_KEY (DeepSeek)
    - 可选: SMTP_* + NOTIFY_TO (m3 发 APV / m4 发 PUB), 没配则模拟跳过 m3
    - 默认 m4 dry_run=True 不真发到平台

会跑通的状态机:
    new → reviewing → approved → (m3 发 APV 或模拟跳过) → publishing → published

每步都打印 verdict / 时间线 / llm_calls, 失败 step 会停在那里.
"""
from __future__ import annotations

import asyncio
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from growthpress.approver import send_approval
from growthpress.db import Database
from growthpress.preflight import ensure, maybe_interactive
from growthpress.publisher.m4_pump import _publish_one
from growthpress.reviewer import review as m2_review
from growthpress.scout_writer import run as m1_run

DB_PATH = Path("data/runs.db")


async def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    topic = sys.argv[1]

    maybe_interactive(source="e2e")
    ensure("llm_api_key", source="e2e")

    async with Database.open(DB_PATH) as db:
        writer = asyncio.create_task(db.writer_loop())
        try:
            # ---------- m1 scout_writer ----------
            print(f"\n>>> [1/4] m1 scout_writer (web_search + 撰写) — topic={topic!r}")
            draft_id, draft = await m1_run(topic, db=db)
            print(
                f"    ✓ draft_id={draft_id} title={draft.title!r} "
                f"sources={len(draft.sources)} body={len(draft.body_md)}c"
            )

            # ---------- m2 reviewer ----------
            print(f"\n>>> [2/4] m2 reviewer (合规 / 质量 / 平台 3 路)")
            verdict = await m2_review(draft_id, db)
            if verdict is None:
                print("    ✗ verdict=None (draft 不存在或 state 不对), 停")
                return
            print(
                f"    compliance: passed={verdict.compliance.passed} "
                f"severity={verdict.compliance.severity}"
            )
            if verdict.quality:
                print(f"    quality   : passed={verdict.quality.passed} score={verdict.quality.score}")
            if verdict.platform:
                print(f"    platform  : passed={verdict.platform.passed}")
            print(f"    ✓ overall passed={verdict.passed}")

            if not verdict.passed:
                final = (await db.read("SELECT state FROM drafts WHERE id=?", (draft_id,)))[0]
                print(f"    ⚠ 未通过, state={final['state']!r}, 端到端在此停")
                await _print_timeline(db, draft_id)
                return

            # ---------- m3 approver ----------
            print(f"\n>>> [3/4] m3 send_approval (SMTP 发 [APV-*] 邮件)")
            try:
                pub_id = await send_approval(draft_id, db)
            except Exception as e:
                print(f"    SMTP 调用异常 ({type(e).__name__}: {e}), 模拟跳过 m3")
                pub_id = None

            if pub_id:
                print(f"    ✓ APV 邮件已发, pub_id={pub_id}")
                print(f"    ⏸ 端到端在此暂停 — 真实流程需要审核员回信 'ok' 才继续 m4")
                print(f"    要继续测 m4, 模拟回信效果:")
                print(f"        sqlite3 data/runs.db \\")
                print(f"          \"UPDATE drafts SET state='publishing' WHERE id='{draft_id}'; \\")
                print(f"           UPDATE approvals SET state='approved', platforms='all', "
                      f"decided_at=datetime('now') WHERE id='{pub_id}';\"")
                print(f"    然后重跑: uv run python scripts/e2e_test.py --continue {draft_id}")
                await _print_timeline(db, draft_id)
                return

            # 模拟 m3 (SMTP 未配): 直接 state → publishing + 写一条 approved approval
            print("    模拟跳过: state → publishing + INSERT approved approval (platforms=all)")
            now_iso = datetime.now(timezone.utc).isoformat()
            expires = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()
            await db.transition(
                draft_id, "approved", "publishing", "m3-mock",
                reason="e2e_test 模拟跳过 SMTP",
            )
            await db.write(
                "INSERT INTO approvals "
                "(id, draft_id, sent_at, expires_at, state, platforms, decided_at) "
                "VALUES (?, ?, ?, ?, 'approved', 'all', ?)",
                (uuid.uuid4().hex[:6], draft_id, now_iso, expires, now_iso),
            )

            # ---------- m4 publisher ----------
            print(f"\n>>> [4/4] m4 publish (dry_run=True, 多平台并发)")
            rows = await db.read(
                "SELECT id, title, summary, body_md, sources FROM drafts WHERE id=?",
                (draft_id,),
            )
            await _publish_one(dict(rows[0]), db, dry_run=True)

            # ---------- 收尾 ----------
            final = (await db.read("SELECT state FROM drafts WHERE id=?", (draft_id,)))[0]
            print(f"\n=== E2E 完成. draft {draft_id} final state: {final['state']!r} ===")
            await _print_timeline(db, draft_id)

            # llm_calls 汇总
            llm = await db.read(
                "SELECT task, model, success, duration_ms FROM llm_calls "
                "WHERE draft_id=? OR draft_id IS NULL ORDER BY at",
                (draft_id,),
            )
            print(f"\n=== llm_calls ({len(llm)} 次) ===")
            for c in llm:
                print(f"    {dict(c)}")
        finally:
            writer.cancel()
            try:
                await writer
            except asyncio.CancelledError:
                pass


async def _print_timeline(db: Database, draft_id: str) -> None:
    logs = await db.read(
        "SELECT from_state, to_state, by_agent, reason, at FROM state_log "
        "WHERE draft_id=? ORDER BY at",
        (draft_id,),
    )
    print(f"\n=== state_log ({len(logs)} 行) ===")
    for r in logs:
        print(f"    {dict(r)}")


if __name__ == "__main__":
    asyncio.run(main())
