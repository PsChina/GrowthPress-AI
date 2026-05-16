"""GrowthPress AI 端到端演示 — 交互问主题 + 平台 + 完整流转可见.

5 个修复:
  1. 询问主题 (input, 非 sys.argv 硬填)
  2. 询问目标平台 (默认全部已装的, 可指定)
  3. APV 邮件: 配 SMTP 真发, 没配 → dump 完整邮件内容到 console (看格式)
  4. m4 默认 dry_run 安全演示, --real 真发 (要 xhs session + 配图)
  5. PUB 邮件 / 每平台结果: 真发或 dump, 失败有原因

用法:
  uv run python scripts/e2e_test.py                  # 全交互
  uv run python scripts/e2e_test.py "topic"          # 主题作参数
  uv run python scripts/e2e_test.py "topic" --real   # 真发 m4 (危险, 要图)
"""
from __future__ import annotations

import asyncio
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from growthpress.approver import send_approval
from growthpress.approver.email_template import build_apv_body, build_apv_subject
from growthpress.core.settings import get_settings
from growthpress.db import Database
from growthpress.preflight import ensure
from growthpress.publisher import discover_platforms
from growthpress.publisher.m4_pump import _publish_one
from growthpress.reviewer import review as m2_review
from growthpress.scout_writer import run as m1_run

DB_PATH = Path("data/runs.db")
DRAFTS_DIR = Path("data/drafts")


def _ask_topic() -> str:
    """argv[1] 优先, tty 时交互问, 非 tty 退出."""
    if len(sys.argv) >= 2 and not sys.argv[1].startswith("--"):
        topic = sys.argv[1]
        print(f"📝 主题 (从 argv): {topic!r}")
        return topic
    if not sys.stdin.isatty():
        print("❌ 非交互模式且没传 argv[1] 主题, 退出.", file=sys.stderr)
        sys.exit(2)
    while True:
        topic = input("\n📝 写什么主题? ").strip()
        if topic:
            return topic
        print("   (空主题, 重新输入)")


def _ask_platforms(available: list[str]) -> list[str]:
    """tty 时问目标平台, 非 tty 默认全选."""
    if not sys.stdin.isatty():
        print(f"\n🎯 平台 (非 tty, 默认全选): {available}")
        return available
    print(f"\n🎯 当前已装的平台: {available}")
    print(f"   回车 = 全发, 或空格分隔指定 (如 'xiaohongshu juejin')")
    val = input("   选择: ").strip()
    if not val:
        return available
    chosen = [p.strip() for p in val.split() if p.strip()]
    invalid = [p for p in chosen if p not in available]
    if invalid:
        print(f"   ⚠ 无效平台 (跳过): {invalid}")
    valid = [p for p in chosen if p in available]
    return valid or available


def _section(title: str) -> None:
    print(f"\n{'━' * 64}")
    print(f"  {title}")
    print(f"{'━' * 64}")


def _dump_email(*, to: str, subject: str, body: str, kind: str) -> None:
    """SMTP 没配时, 把邮件完整内容打到 console 让用户看见."""
    print(f"  🔄 [{kind}] 模拟邮件 (SMTP 未配, 仅展示内容, 没真发):")
    print(f"  ┌─ To      : {to or '(未配 NOTIFY_TO)'}")
    print(f"  ├─ Subject : {subject}")
    print(f"  ├─ Body:")
    for line in body.splitlines() or ["(空)"]:
        print(f"  │  {line}")
    print(f"  └{'─' * 60}")


def _maybe_continue_id() -> str | None:
    """--continue <draft_id> 跳 m1, 复用已有 draft 继续测 m2/m3/m4."""
    for i, a in enumerate(sys.argv):
        if a == "--continue" and i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return None


async def main() -> None:
    ensure("llm_api_key", source="e2e")
    real_mode = "--real" in sys.argv
    continue_id = _maybe_continue_id()

    print()
    print("═" * 64)
    print("  GrowthPress AI 端到端流程演示")
    print("═" * 64)

    if continue_id:
        print(f"⏭  --continue {continue_id} 模式: 跳过 m1, 复用已有 draft")
        topic = None
    else:
        topic = _ask_topic()

    s = get_settings()
    smtp_user = s.smtp_user
    smtp_pass = s.smtp_pass.get_secret_value()
    notify_to = s.notify_to
    has_smtp = bool(smtp_user and smtp_pass and notify_to)

    print()
    print(f"=== 配置检测 ===")
    print(f"  topic       : {topic!r}")
    print(f"  SMTP        : {'✓ 配齐 (会真发邮件)' if has_smtp else '✗ 未配 (邮件 dump 到 console)'}")
    print(f"  NOTIFY_TO   : {notify_to or '(未配)'}")
    print(f"  m4 模式     : {'⚠ --real 真发到平台 (要 xhs session + 配图)' if real_mode else 'dry_run (假装发, 安全)'}")

    async with Database.open(DB_PATH) as db:
        writer = asyncio.create_task(db.writer_loop())
        try:
            # ─── 1/4 m1 scout_writer (或 --continue 跳过) ─────────
            if continue_id:
                _section(f"[1/4] m1 跳过 — 复用 draft {continue_id}")
                rows = await db.read(
                    "SELECT id, topic, title, body_md, summary, sources, state "
                    "FROM drafts WHERE id=?", (continue_id,)
                )
                if not rows:
                    print(f"  ✗ draft {continue_id!r} 不存在, 退出")
                    return
                d = dict(rows[0])
                draft_id = d["id"]
                # 装回 DraftSchema 兼容后续代码
                from growthpress.scout_writer import DraftSchema, Source
                import json as _json
                src_list = _json.loads(d["sources"] or "[]") if d["sources"] else []
                draft = DraftSchema(
                    title=d["title"] or "(无标题)",
                    tags=["continue-mode"],   # drafts 表无 tags 列, dummy 占位 (m2/m3/m4 不读 tags)
                    body_md=d["body_md"] or "",
                    summary=d["summary"] or "(无摘要)",
                    sources=[Source(**s) for s in src_list],
                )
                print(f"  ✓ 复用 draft_id={draft_id} state={d['state']}")
                print(f"    title : {draft.title!r}")
                print(f"    body  : {len(draft.body_md)} chars")
                # state 不在 new 时 reset (m2 需要 new 才能 CAS 锁)
                if d["state"] != "new":
                    print(f"  ⚠ state={d['state']!r} 不在 'new', reset 让 m2 能跑")
                    await db.write("UPDATE drafts SET state='new' WHERE id=?", (draft_id,))
            else:
                _section("[1/4] m1 scout_writer — DeepSeek web_search 调研 + 撰写")
                print("  (3-4 min, 真调 LLM 烧 token)")
                draft_id, draft = await m1_run(topic, db=db)
                print(f"  ✓ draft_id={draft_id}")
                print(f"    title  : {draft.title!r}")
                print(f"    body   : {len(draft.body_md)} chars")
                print(f"    sources: {len(draft.sources)} 个权威来源")
                for i, src in enumerate(draft.sources[:5], 1):
                    print(f"      [{i}] {src.title} — {src.url}")
                print(f"    落盘   : data/drafts/{draft_id}.md")

            # ─── 2/4 m2 reviewer ──────────────────────────────────
            _section("[2/4] m2 reviewer — 合规 / 质量 / 平台 三路审核")
            verdict = await m2_review(draft_id, db)
            if verdict is None:
                print("  ✗ verdict=None (draft 不在 new state), 端到端停")
                return
            print(f"  compliance: {'✓' if verdict.compliance.passed else '✗'} "
                  f"severity={verdict.compliance.severity}")
            if verdict.quality:
                print(f"  quality   : {'✓' if verdict.quality.passed else '✗'} "
                      f"score={verdict.quality.score}/100")
            if verdict.platform:
                print(f"  platform  : {'✓' if verdict.platform.passed else '✗'}")
            print(f"  overall   : {'✓ passed' if verdict.passed else '✗ failed'}")

            if not verdict.passed:
                final = (await db.read("SELECT state FROM drafts WHERE id=?",
                                       (draft_id,)))[0]
                print(f"  ⚠ 未过 m2, state={final['state']!r}, 端到端在此停")
                return

            # ─── 询问目标平台 (m2 过了才问) ───────────────────────
            available = list(discover_platforms().keys())
            if not available:
                print(f"\n  ⚠ 无可用 publisher (entry_point 没找到), 端到端停")
                return
            target_platforms = _ask_platforms(available)
            platforms_csv = ",".join(target_platforms)

            # ─── 3/4 m3 approver ──────────────────────────────────
            _section("[3/4] m3 approver — APV 审批邮件 (SMTP 真发 or console dump)")

            pub_id = uuid.uuid4().hex[:6]
            expires_at = datetime.now(timezone.utc) + timedelta(hours=24)

            if has_smtp:
                # 真发模式
                print("  → SMTP 已配, 真发 APV 邮件")
                try:
                    real_pub_id = await send_approval(draft_id, db)
                    if real_pub_id:
                        pub_id = real_pub_id
                        print(f"  ✓ APV 邮件已发到 {notify_to}, pub_id={pub_id}")
                        print(f"  ⏸ 端到端在此暂停 — 真实流程需你回信 'ok juejin csdn'")
                        print(f"     (m5 mailbox 30s 内收到 → 触发 m4)")
                        print(f"")
                        print(f"  💡 想跳过等真邮件, 模拟回信效果:")
                        print(f"     sqlite3 data/runs.db \"\\")
                        print(f"       UPDATE drafts SET state='publishing' WHERE id='{draft_id}'; \\")
                        print(f"       UPDATE approvals SET state='approved', "
                              f"platforms='{platforms_csv}', "
                              f"decided_at=datetime('now') WHERE id='{pub_id}';\"")
                        print(f"     然后重跑: uv run growthpress  # daemon m4_pump 60s 内拉")
                        return
                    else:
                        print("  ✗ send_approval 返 None, 落 console")
                except Exception as e:
                    print(f"  ✗ send_approval 异常: {type(e).__name__}: {e}")
                    print(f"    → 落 console 模拟")

            # SMTP 没配 / 失败 → dump APV 邮件内容 + 模拟 ok 进 m4
            apv_subject = build_apv_subject(pub_id, draft.title)
            apv_body = build_apv_body(
                pub_id=pub_id,
                title=draft.title,
                summary=draft.summary,
                body_md=draft.body_md,
                draft_path=DRAFTS_DIR / f"{draft_id}.md",
                expires_at=expires_at,
                platforms=tuple(target_platforms),
            )
            _dump_email(to=notify_to or "<未配>", subject=apv_subject,
                        body=apv_body, kind="APV")

            print(f"\n  🔄 模拟人工回信 'ok' → state 变迁:")
            now_iso = datetime.now(timezone.utc).isoformat()
            await db.transition(
                draft_id, "approved", "publishing", "m3-mock",
                reason=f"e2e 模拟 SMTP 未配, 人工 ok platforms={platforms_csv}",
            )
            await db.write(
                "INSERT INTO approvals "
                "(id, draft_id, sent_at, expires_at, state, platforms, decided_at) "
                "VALUES (?, ?, ?, ?, 'approved', ?, ?)",
                (pub_id, draft_id, now_iso, expires_at.isoformat(),
                 platforms_csv, now_iso),
            )
            print(f"  ✓ approvals.platforms={platforms_csv} state=approved")
            print(f"  ✓ drafts.state approved → publishing")

            # ─── 4/4 m4 publisher ─────────────────────────────────
            _section("[4/4] m4 publisher — 多平台并发发布")
            print(f"  dry_run = {not real_mode}")
            if real_mode:
                print(f"  ⚠ 真发模式. 需要 plugins/<platform>/.session cookies + 配图 (Content.media).")
                print(f"  ⚠ 当前 e2e Content.media=[] (无图), 平台调用大概率 fail.")

            rows = await db.read(
                "SELECT id, title, summary, body_md, sources FROM drafts WHERE id=?",
                (draft_id,),
            )
            await _publish_one(dict(rows[0]), db, dry_run=not real_mode)

            # 展示 publications + PUB 邮件内容
            pubs = await db.read(
                "SELECT id, platform, state, url, published_at "
                "FROM publications WHERE draft_id=? ORDER BY published_at DESC NULLS LAST",
                (draft_id,),
            )
            print(f"\n  publications 表 ({len(pubs)} 行):")
            for p in pubs:
                pd = dict(p)
                mark = "✓" if pd["state"] == "published" else "✗"
                print(f"    {mark} [{pd['platform']:12s}] state={pd['state']:10s} "
                      f"url={pd.get('url') or '(无)'}")
                if pd["state"] == "published" and not has_smtp:
                    # 没配 SMTP, dump PUB
                    pub_subject = f"[PUB-{pd['id']}-{pd['platform']}] 已发布: 《{draft.title}》"
                    pub_body = (
                        f"✅ 已发布到 {pd['platform']}\n"
                        f"URL: {pd.get('url') or '(等平台返回)'}\n"
                        f"发布时间: {pd.get('published_at')}\n"
                        f"撤销窗口: 24h (回信任意内容即触发撤销)\n"
                    )
                    _dump_email(to=notify_to or "<未配>", subject=pub_subject,
                                body=pub_body, kind="PUB")

            # ─── 收尾 ────────────────────────────────────────────
            final = (await db.read("SELECT state FROM drafts WHERE id=?",
                                   (draft_id,)))[0]
            _section(f"完成 — draft {draft_id} final state: {final['state']!r}")

            print(f"\n  state_log 时间线:")
            for r in await db.read(
                "SELECT from_state, to_state, by_agent, reason, at FROM state_log "
                "WHERE draft_id=? ORDER BY at",
                (draft_id,),
            ):
                rd = dict(r)
                arrow = f"{rd['from_state'] or 'NULL':12s} → {rd['to_state']:12s}"
                print(f"    [{rd['at'][:19]}] {arrow}  by={rd['by_agent']:12s}  "
                      f"reason={(rd['reason'] or '')[:50]}")

            llm = await db.read(
                "SELECT task, model, success, duration_ms FROM llm_calls "
                "WHERE draft_id=? OR draft_id IS NULL ORDER BY at DESC LIMIT 10",
                (draft_id,),
            )
            print(f"\n  最近 10 次 LLM 调用 (含 draft_id=NULL 的 m1 调研):")
            for c in llm:
                cd = dict(c)
                mark = "✓" if cd["success"] else "✗"
                print(f"    {mark} {cd['task']:20s} {cd['model']:5s} "
                      f"{cd['duration_ms']:>6}ms")
        finally:
            writer.cancel()
            try:
                await writer
            except asyncio.CancelledError:
                pass


if __name__ == "__main__":
    asyncio.run(main())
