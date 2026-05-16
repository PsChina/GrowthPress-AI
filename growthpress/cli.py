"""GrowthPress 工具腰带 CLI — 给外部 claude CLI / 终端用户用.

新架构: GrowthPress 不再内置 LLM, "大脑"是外部 claude CLI. 工作流是
  claude (调研 / 找图 / 写文) → 调本 CLI 落库 → 调本 CLI 发布.

子命令:
  draft new            落 draft 到 db. 必要: title / body / media (≥1 张). 可选: topic / state.
  draft publish        把已有 draft state 改 publishing → m4_pump 60s 内拉起发布.
  draft publish-now    同步直接调 publisher 真发, 不等 daemon 轮询. (用于 Claude 主动触发)
  draft list           列 draft, 默认按 updated_at 倒序前 20.
  draft show           看 draft 详情 (title / body / media / state / 时间线).
  platforms            列已装的 publisher (entry_points 扫描).

设计:
  - 每个命令独立短命: open db → 干活 → close. 不依赖 daemon 起着.
  - stdout 默认人类友好, 加 --json 出 JSON 给 Claude 解析.

跑法:
  python -m growthpress draft new --title ... --body ... --media a.jpg --media b.jpg
  python -m growthpress draft publish <id> --platforms xiaohongshu
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .db import Database
from .publisher import discover_platforms
from .publisher.base import Content, ContentType

log = logging.getLogger("growthpress.cli")

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "runs.db"


# ---------- helpers ----------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _short_id() -> str:
    return uuid.uuid4().hex[:8]


def _print(obj: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps(obj, ensure_ascii=False, default=str))
    else:
        for k, v in obj.items():
            print(f"  {k}: {v}")


# ---------- subcommands ----------

async def cmd_draft_new(args: argparse.Namespace) -> int:
    media_paths = [Path(p).expanduser().resolve() for p in (args.media or [])]
    missing = [str(p) for p in media_paths if not p.is_file()]
    if missing:
        print(f"✗ 这些 media 文件不存在: {missing}", file=sys.stderr)
        return 2

    draft_id = _short_id()
    async with Database.open(DB_PATH) as db:
        writer = asyncio.create_task(db.writer_loop())
        try:
            now = _now_iso()
            await db.write(
                """INSERT INTO drafts
                   (id, topic, title, body_md, summary, sources, media,
                    state, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    draft_id,
                    args.topic or "",
                    args.title,
                    args.body,
                    args.summary or "",
                    "[]",
                    json.dumps([str(p) for p in media_paths], ensure_ascii=False),
                    args.state,
                    now,
                    now,
                ),
            )
            await db.log_initial_state(
                draft_id, to_state=args.state, by_agent="claude-cli",
                reason=f"draft new by cli (title={args.title[:60]!r})",
            )
        finally:
            writer.cancel()
            try:
                await writer
            except asyncio.CancelledError:
                pass

    _print({
        "ok": True,
        "draft_id": draft_id,
        "state": args.state,
        "title": args.title,
        "media_count": len(media_paths),
    }, args.json)
    return 0


async def cmd_draft_list(args: argparse.Namespace) -> int:
    async with Database.open(DB_PATH) as db:
        if args.state:
            rows = await db.read(
                "SELECT id, topic, title, state, updated_at FROM drafts "
                "WHERE state=? ORDER BY updated_at DESC LIMIT ?",
                (args.state, args.limit),
            )
        else:
            rows = await db.read(
                "SELECT id, topic, title, state, updated_at FROM drafts "
                "ORDER BY updated_at DESC LIMIT ?",
                (args.limit,),
            )

    drafts = [dict(r) for r in rows]
    if args.json:
        print(json.dumps(drafts, ensure_ascii=False, default=str))
    else:
        if not drafts:
            print("(无 draft)")
            return 0
        print(f"{'id':10s} {'state':14s} {'updated':21s} title")
        for d in drafts:
            print(f"  {d['id']:8s} {d['state']:14s} {d['updated_at'][:19]:21s} "
                  f"{(d['title'] or '')[:60]}")
    return 0


async def cmd_draft_show(args: argparse.Namespace) -> int:
    async with Database.open(DB_PATH) as db:
        rows = await db.read(
            "SELECT * FROM drafts WHERE id=?", (args.draft_id,)
        )
        if not rows:
            print(f"✗ draft {args.draft_id!r} 不存在", file=sys.stderr)
            return 1
        draft = dict(rows[0])
        # 解析 JSON 字段方便看
        try:
            draft["media"] = json.loads(draft.get("media") or "[]")
        except Exception:
            pass
        try:
            draft["sources"] = json.loads(draft.get("sources") or "[]")
        except Exception:
            pass

        log_rows = await db.read(
            "SELECT from_state, to_state, by_agent, at, reason FROM state_log "
            "WHERE draft_id=? ORDER BY at",
            (args.draft_id,),
        )
        timeline = [dict(r) for r in log_rows]

    if args.json:
        print(json.dumps({"draft": draft, "timeline": timeline},
                         ensure_ascii=False, default=str))
    else:
        for k in ("id", "state", "topic", "title", "summary"):
            print(f"  {k}: {draft.get(k)}")
        body = draft.get("body_md") or ""
        print(f"  body_md: ({len(body)} chars)")
        print("\n".join(f"    {line}" for line in body.splitlines()[:8]))
        if len(body.splitlines()) > 8:
            print(f"    ... (+{len(body.splitlines()) - 8} more lines)")
        print(f"  media ({len(draft.get('media', []))} 张):")
        for p in draft.get("media", []):
            print(f"    - {p}")
        print(f"  state_log ({len(timeline)} 条):")
        for t in timeline:
            print(f"    [{t['at'][:19]}] {t['from_state'] or 'NULL':12s} → "
                  f"{t['to_state']:12s} by {t['by_agent']}")
    return 0


async def cmd_draft_publish_now(args: argparse.Namespace) -> int:
    """同步直接调 publisher 真发, 不走 daemon m4_pump 轮询.

    流程:
      1. 拿 draft (含 media 本地路径)
      2. discover_platforms() 找 target publisher
      3. 直接 await publisher.publish(content, dry_run=...)
      4. 落 publications 表 + state 变迁 (publishing → published / human_queue)
    """
    async with Database.open(DB_PATH) as db:
        writer = asyncio.create_task(db.writer_loop())
        try:
            rows = await db.read(
                "SELECT id, title, body_md, summary, media, state FROM drafts WHERE id=?",
                (args.draft_id,),
            )
            if not rows:
                print(f"✗ draft {args.draft_id!r} 不存在", file=sys.stderr)
                return 1
            d = dict(rows[0])
            try:
                media_paths_str = json.loads(d.get("media") or "[]")
            except Exception:
                media_paths_str = []
            media_paths = [Path(p) for p in media_paths_str if Path(p).is_file()]
            if len(media_paths) != len(media_paths_str):
                print(f"⚠ 部分 media 文件丢失: db={len(media_paths_str)} 真实={len(media_paths)}",
                      file=sys.stderr)

            all_pubs = discover_platforms()
            target_names = args.platforms or list(all_pubs.keys())
            publishers = [all_pubs[n] for n in target_names if n in all_pubs]
            if not publishers:
                print(f"✗ 无可用 publisher (target={target_names}, "
                      f"available={list(all_pubs.keys())})", file=sys.stderr)
                return 3

            # state 变迁 (允许从 draft / approved / publishing 任一进入 publishing)
            if d["state"] != "publishing":
                await db.transition(
                    args.draft_id, d["state"], "publishing", "claude-cli",
                    reason=f"publish-now to {target_names}",
                )

            content: Content = {
                "type": ContentType.IMAGE_NOTE,
                "title": d["title"] or "",
                "body": d["body_md"] or "",
                "media": media_paths,
                "tags": [],
                "meta": {},
            }

            results = []
            for pub in publishers:
                print(f"→ {pub.name} (dry_run={not args.real})...", file=sys.stderr)
                try:
                    r = await pub.publish(content, dry_run=not args.real)
                except Exception as e:
                    r = {
                        "success": False, "dry_run": not args.real,
                        "platform": pub.name, "url": None, "screenshot": None,
                        "error": type(e).__name__,
                        "error_detail": str(e)[:300],
                        "elapsed_sec": 0.0,
                    }
                results.append(r)

                pub_id = _short_id()
                await db.write(
                    "INSERT INTO publications "
                    "(id, draft_id, platform, post_id, url, state, "
                    "published_at, retract_window_until) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        pub_id, args.draft_id, pub.name, None,
                        r.get("url"),
                        "published" if r.get("success") else "failed",
                        _now_iso() if r.get("success") else None,
                        None,
                    ),
                )

            ok = sum(1 for r in results if r.get("success"))
            target_state = "published" if ok > 0 else "human_queue"
            await db.transition(
                args.draft_id, "publishing", target_state, "claude-cli",
                reason=f"{ok}/{len(results)} 平台成功",
            )
        finally:
            writer.cancel()
            try:
                await writer
            except asyncio.CancelledError:
                pass

    out = {
        "ok": ok > 0,
        "draft_id": args.draft_id,
        "real": args.real,
        "final_state": target_state,
        "results": [
            {
                "platform": r["platform"], "success": r.get("success"),
                "url": r.get("url"), "error": r.get("error"),
                "error_detail": r.get("error_detail"),
                "elapsed_sec": r.get("elapsed_sec"),
            }
            for r in results
        ],
    }
    if args.json:
        print(json.dumps(out, ensure_ascii=False, default=str))
    else:
        print(f"\n结果: {ok}/{len(results)} 平台成功, final_state={target_state}")
        for r in results:
            mark = "✓" if r.get("success") else "✗"
            url = r.get("url") or "(无)"
            err = f" — {r.get('error')}: {r.get('error_detail')}" if not r.get("success") else ""
            print(f"  {mark} [{r['platform']}] url={url}{err}")
    return 0 if ok > 0 else 4


async def cmd_draft_publish(args: argparse.Namespace) -> int:
    """异步路径: 把 draft state → publishing, 写一条 approvals 让 m4_pump 拉.

    比 publish-now 多一步 (state + approvals), 走 daemon 轮询; 不会卡 cli.
    Claude 想"火-and-forget" 时用这个, 想立刻看结果用 publish-now.
    """
    async with Database.open(DB_PATH) as db:
        writer = asyncio.create_task(db.writer_loop())
        try:
            rows = await db.read("SELECT state FROM drafts WHERE id=?", (args.draft_id,))
            if not rows:
                print(f"✗ draft {args.draft_id!r} 不存在", file=sys.stderr)
                return 1
            from_state = rows[0]["state"]
            if from_state == "publishing":
                print(f"  draft 已在 publishing, 等 daemon 拉")
            else:
                ok_t = await db.transition(
                    args.draft_id, from_state, "publishing", "claude-cli",
                    reason=f"queue publish to {args.platforms}",
                )
                if not ok_t:
                    print(f"✗ state 变迁失败 (并发?)", file=sys.stderr)
                    return 5

            # 写 approvals 让 m4_pump 知道要发哪些平台
            pub_id = _short_id()
            await db.write(
                "INSERT INTO approvals "
                "(id, draft_id, sent_at, expires_at, state, platforms, decided_at) "
                "VALUES (?, ?, ?, ?, 'approved', ?, ?)",
                (
                    pub_id, args.draft_id, _now_iso(), _now_iso(),
                    ",".join(args.platforms) if args.platforms else "all",
                    _now_iso(),
                ),
            )
        finally:
            writer.cancel()
            try:
                await writer
            except asyncio.CancelledError:
                pass

    _print({
        "ok": True,
        "draft_id": args.draft_id,
        "state": "publishing",
        "platforms": args.platforms,
        "note": "daemon m4_pump 60s 内拉起发布",
    }, args.json)
    return 0


def cmd_platforms(args: argparse.Namespace) -> int:
    pubs = discover_platforms()
    if args.json:
        print(json.dumps({
            name: {"types": [t.value for t in p.supported_types]}
            for name, p in pubs.items()
        }, ensure_ascii=False))
    else:
        if not pubs:
            print("(无可用 publisher, 装 entry_point 后再跑)")
            return 0
        for name, p in pubs.items():
            types = ",".join(t.value for t in p.supported_types)
            print(f"  {name:14s} types=[{types}]")
    return 0


# ---------- argparse ----------

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="python -m growthpress",
        description="GrowthPress 工具腰带 CLI (给 claude / 终端用户)",
    )
    ap.add_argument("--json", action="store_true", help="输出 JSON 给 Claude 解析")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_draft = sub.add_parser("draft", help="管理 draft")
    psub = p_draft.add_subparsers(dest="sub", required=True)

    p_new = psub.add_parser("new", help="落一个新 draft")
    p_new.add_argument("--title", required=True)
    p_new.add_argument("--body", required=True, help="markdown 正文")
    p_new.add_argument("--media", action="append", default=[],
                       help="本地图片路径, 可多次. image_note 需 1-9 张")
    p_new.add_argument("--topic", default="", help="主题字符串 (跟踪用)")
    p_new.add_argument("--summary", default="")
    p_new.add_argument("--state", default="draft",
                       help="入库初始 state, 默认 draft. 可填 approved 跳过审批")

    p_list = psub.add_parser("list", help="列 draft")
    p_list.add_argument("--state", default=None, help="只看某 state")
    p_list.add_argument("--limit", type=int, default=20)

    p_show = psub.add_parser("show", help="详情 + state_log")
    p_show.add_argument("draft_id")

    p_pub = psub.add_parser("publish", help="放进发布队列, daemon 拉")
    p_pub.add_argument("draft_id")
    p_pub.add_argument("--platforms", nargs="+", default=[],
                       help="目标平台, 空 = 全部已装")

    p_now = psub.add_parser("publish-now", help="同步直接调 publisher 真发")
    p_now.add_argument("draft_id")
    p_now.add_argument("--platforms", nargs="+", default=[])
    p_now.add_argument("--real", action="store_true",
                       help="默认 dry_run, 加 --real 才真发到平台")

    sub.add_parser("platforms", help="列已装的 publisher")

    return ap


def _route(args: argparse.Namespace):
    if args.cmd == "draft":
        return {
            "new": cmd_draft_new,
            "list": cmd_draft_list,
            "show": cmd_draft_show,
            "publish": cmd_draft_publish,
            "publish-now": cmd_draft_publish_now,
        }[args.sub]
    if args.cmd == "platforms":
        return cmd_platforms
    raise SystemExit(2)


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.WARNING,
        format="[%(name)s] %(message)s",
    )
    fn = _route(args)
    if asyncio.iscoroutinefunction(fn):
        return asyncio.run(fn(args))
    return fn(args)


if __name__ == "__main__":
    sys.exit(main())
