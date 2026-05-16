"""m4 publisher 调度: 周期扫 state='publishing' → 多平台并发发布.

按 memory architecture / 5 个踩坑 #2 #4: 单平台 wait_for 超时不挂 batch,
gather return_exceptions 让一路挂不影响其他.

W1 默认 dry_run=True (流程跑通不真发). 真发需要:
  1. xhs Publisher class 适配 (LegacyImagePublisher → XhsPublisher class)
  2. 提供配图 (Content.media), W1 没有图源默认空
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..db import Database
from .agent import discover_platforms
from .base import Content, ContentType, PublishResult
from .pub_email import send_pub_notification

log = logging.getLogger("growthpress.publisher.m4")

M4_PUMP_INTERVAL_SEC = 60
SINGLE_PLATFORM_TIMEOUT_SEC = 300
RETRACT_WINDOW_HOURS = 24


async def m4_pump_runs(db: Database, *, dry_run: bool = True) -> None:
    """长生命周期 task. 60s 扫 state='publishing' 调多平台 publish.

    Args:
        dry_run: True 时 publisher 不真发 (W1 默认). 上线真发改 False.
    """
    log.info(
        f"[m4_pump] start, interval={M4_PUMP_INTERVAL_SEC}s, "
        f"single_platform_timeout={SINGLE_PLATFORM_TIMEOUT_SEC}s, dry_run={dry_run}"
    )
    while True:
        try:
            rows = await db.read(
                "SELECT id, title, summary, body_md, sources, media "
                "FROM drafts WHERE state='publishing' ORDER BY updated_at LIMIT 5"
            )
            for r in rows:
                try:
                    await _publish_one(dict(r), db, dry_run=dry_run)
                except Exception as e:
                    log.error(
                        f"[m4_pump] _publish_one failed for {r['id']}: {e!r}",
                        exc_info=True,
                    )
        except Exception as e:
            log.error(f"[m4_pump] scan failed: {e!r}", exc_info=True)
        await asyncio.sleep(M4_PUMP_INTERVAL_SEC)


async def _publish_one(draft: dict, db: Database, *, dry_run: bool) -> None:
    """单 draft 多平台发布 + 落 publications 表 + state 变迁."""
    draft_id = draft["id"]

    # 1. 拿审批阶段记录的 platforms (m3 写入 approvals.platforms)
    apv_rows = await db.read(
        "SELECT platforms FROM approvals WHERE draft_id=? AND state='approved' "
        "ORDER BY decided_at DESC LIMIT 1",
        (draft_id,),
    )
    if not apv_rows:
        log.warning(
            f"[m4] draft {draft_id} 在 publishing 但无 approved approval → human_queue"
        )
        await db.transition(
            draft_id, "publishing", "human_queue", "m4",
            reason="缺 approved approval",
        )
        return

    platforms_csv = apv_rows[0]["platforms"] or "all"
    target_platforms = [p.strip() for p in platforms_csv.split(",") if p.strip()]

    # 2. discover + 按 target 筛
    all_pubs = discover_platforms()
    if "all" in target_platforms or not target_platforms:
        publishers = list(all_pubs.values())
    else:
        publishers = [all_pubs[n] for n in target_platforms if n in all_pubs]

    if not publishers:
        log.error(
            f"[m4] draft {draft_id} 无可用 publisher "
            f"(target={target_platforms}, available={list(all_pubs.keys())}) → human_queue"
        )
        await db.transition(
            draft_id, "publishing", "human_queue", "m4",
            reason=f"无 publisher: target={target_platforms}, available={list(all_pubs.keys())}",
        )
        return

    log.info(
        f"[m4] draft {draft_id!r} → publishers={[p.name for p in publishers]} "
        f"(dry_run={dry_run})"
    )

    # 3. 造 Content. media 是 m1 阶段已下载到本地的图片路径 list.
    # 如果 m1 没找到图 (draft.media=[]), publisher 会按平台约束返失败 (例: xhs invalid_input).
    try:
        media_paths = json.loads(draft.get("media") or "[]")
    except (json.JSONDecodeError, TypeError):
        media_paths = []
    # 只保留实际存在的文件 (m1 下载有失败、文件被手动删等情况)
    media_paths = [p for p in media_paths if Path(p).is_file()]

    content: Content = {
        "type": ContentType.IMAGE_NOTE,
        "title": draft["title"] or "",
        "body": draft["body_md"] or "",
        "media": media_paths,
        "tags": [],
        "meta": {},
    }

    # 4. 并发发布 + 单平台 wait_for 超时 (一路挂不影响其他)
    results = await asyncio.gather(
        *(_safe_publish(p, content, dry_run) for p in publishers)
    )

    # 5. 落 publications 表
    now = datetime.now(timezone.utc).isoformat()
    retract_until = (
        datetime.now(timezone.utc) + timedelta(hours=RETRACT_WINDOW_HOURS)
    ).isoformat()
    for r in results:
        publication_id = uuid.uuid4().hex[:8]
        await db.write(
            "INSERT INTO publications "
            "(id, draft_id, platform, post_id, url, state, published_at, retract_window_until) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                publication_id,
                draft_id,
                r.get("platform", "unknown"),
                None,
                r.get("url"),
                "published" if r.get("success") else "failed",
                now if r.get("success") else None,
                retract_until,
            ),
        )
        if r.get("success"):
            log.info(
                f"[m4] {draft_id} ✓ {r['platform']} {r.get('url') or '(dry_run)'}"
            )
            # 发 PUB 通知邮件 (dry_run / 未配 SMTP 时优雅跳过)
            await send_pub_notification(
                publication_id=publication_id,
                draft_id=draft_id,
                title=draft.get("title") or "",
                platform=r.get("platform", "unknown"),
                url=r.get("url"),
                published_at=now,
                retract_until=retract_until,
                dry_run=dry_run,
            )
        else:
            log.warning(
                f"[m4] {draft_id} ✗ {r['platform']}: "
                f"{r.get('error')}: {r.get('error_detail')}"
            )

    # 6. state 变迁
    n_success = sum(1 for r in results if r.get("success"))
    if n_success > 0:
        await db.transition(
            draft_id, "publishing", "published", "m4",
            reason=f"{n_success}/{len(results)} 平台成功",
        )
        log.info(f"[m4] {draft_id} → published ({n_success}/{len(results)})")
        # TODO W3+: 发 [PUB-{pub_id}] 通知邮件 (复用 m3 SMTP 风格)
    else:
        await db.transition(
            draft_id, "publishing", "human_queue", "m4",
            reason=f"全平台失败 ({len(results)} 个)",
        )
        log.error(
            f"[m4] {draft_id} 全平台失败 → human_queue "
            f"({[(r['platform'], r.get('error')) for r in results]})"
        )


async def _safe_publish(pub, content: Content, dry_run: bool) -> PublishResult:
    """单平台调用 + wait_for 超时 + try/except. 不抛, 总是返 PublishResult dict."""
    try:
        return await asyncio.wait_for(
            pub.publish(content, dry_run=dry_run),
            timeout=SINGLE_PLATFORM_TIMEOUT_SEC,
        )
    except asyncio.TimeoutError:
        return {
            "success": False, "dry_run": dry_run, "platform": pub.name,
            "url": None, "screenshot": None,
            "error": "timeout",
            "error_detail": f"single-platform timeout {SINGLE_PLATFORM_TIMEOUT_SEC}s",
            "elapsed_sec": float(SINGLE_PLATFORM_TIMEOUT_SEC),
        }
    except Exception as e:
        return {
            "success": False, "dry_run": dry_run, "platform": pub.name,
            "url": None, "screenshot": None,
            "error": type(e).__name__,
            "error_detail": str(e)[:300],
            "elapsed_sec": 0.0,
        }
