"""daemon task: 扫 drafts.state='revising' → 起 claude CLI subprocess 让 Claude 改稿.

闭环:
  用户邮件回 "改 <意见>" → m5 mailbox → approver._handle_reject 改 state=revising
  → 本 dispatcher 60s 内扫到 → 起 claude subprocess → Claude 改稿 + 重新 send-apv
  → state 回到 pending_human → 用户再次审批

防重:
  in-memory _running 集合记本次 daemon 起过的 draft. claude 跑完后 state 应该
  脱离 revising (变 pending_human / human_queue). 仍卡 revising 表示改稿失败,
  本次 daemon 不重试 (避免烧 quota), 等下次 daemon 重启或人工介入.

  如果 daemon 进程重启 (launchd KeepAlive), set 清空, 卡死的 draft 会被重试一次,
  这是优雅退化 — 偶发瞬态错误能自愈, 持续错误最多浪费 1 次重启周期.

失败兜底:
  claude 退出码 != 0 / timeout → dispatcher 把 state 转 human_queue + log error.
  (告警邮件由 watchdog 统一发, 不在本 task)
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from ..db import Database
from .claude_subprocess import DEFAULT_TIMEOUT_SEC, run_claude

log = logging.getLogger("growthpress.runners.revising")

SCAN_INTERVAL_SEC = 60
CLAUDE_TIMEOUT_SEC = DEFAULT_TIMEOUT_SEC   # 30 min


async def _build_revise_prompt(db: Database, draft_id: str) -> str | None:
    """根据 draft + approvals.feedback 拼 Claude 改稿 prompt.

    Returns None 表示数据缺失不该起 claude (例: draft 不存在 / 没 feedback).
    """
    rows = await db.read(
        "SELECT id, topic, title, body_md, media FROM drafts WHERE id=?",
        (draft_id,),
    )
    if not rows:
        log.warning(f"[revising] draft {draft_id} 不存在, 跳过")
        return None
    d = dict(rows[0])

    apv_rows = await db.read(
        "SELECT feedback, decided_at FROM approvals "
        "WHERE draft_id=? AND state='rejected' ORDER BY decided_at DESC LIMIT 1",
        (draft_id,),
    )
    feedback = (dict(apv_rows[0])["feedback"] if apv_rows else "") or "(无具体反馈)"

    try:
        media_paths = json.loads(d.get("media") or "[]")
    except Exception:
        media_paths = []

    body_excerpt = (d.get("body_md") or "")[:500]
    media_list = "\n".join(f"  - {p}" for p in media_paths) or "  (无)"

    return (
        f"Claude, 这是一个**改稿任务** (不是新发任务). 用户在 APV 邮件审批时退回了草稿,\n"
        f"反馈具体意见. 请按反馈改完后用 cli 更新 draft + 重发 APV.\n\n"
        f"=== 原 draft ===\n"
        f"draft_id : {draft_id}\n"
        f"topic    : {d.get('topic') or '(无)'}\n"
        f"title    : {d.get('title') or '(无)'}\n"
        f"body_md (前 500 字):\n{body_excerpt}\n"
        f"media:\n{media_list}\n\n"
        f"=== 用户反馈 ===\n"
        f"{feedback}\n\n"
        f"=== 期望动作 ===\n"
        f"1. 按反馈调整 title / body / media (媒体若需更换, 重新走 WebSearch + WebFetch + curl 找图流程).\n"
        f"2. 用 `python -m growthpress draft update {draft_id} --title ... --body ... [--media ...]` 落库\n"
        f"   (如果 cli 暂无 update 子命令, 退化方案: `draft new --topic '{d.get('topic') or ''}' --state approved`\n"
        f"   生成新 draft, 旧 draft 用 `sqlite3 data/runs.db \"UPDATE drafts SET state='archived' WHERE id='{draft_id}'\"` 归档).\n"
        f"3. 运行 `python -m growthpress draft publish-now <draft_id> --platforms xiaohongshu` 出 dry_run 预览.\n"
        f"4. Read 截图人工检查后, 运行 `python -m growthpress draft send-apv <draft_id>` 重发 APV.\n"
        f"5. 退出 (daemon 接管后续: 用户再次回信 → state 变迁 → m4_pump 真发).\n\n"
        f"参考 .claude/skills/growthpress-post/SKILL.md.\n"
        f"约束: 改稿次数已 +1, 同一 draft 最多 2 轮改稿超过自动转 human_queue 不再派工."
    )


async def revising_dispatcher_run(db: Database) -> None:
    """长生命周期 task. 60s 扫 drafts.state='revising', 串行起 claude subprocess.

    串行 (一次只跑一个 claude subprocess) 是有意的:
      - claude OAuth quota 共享, 并行多个会撞 rate limit
      - 改稿任务一般 5-10 分钟, 串行能容忍
      - daemon 起 1 个 claude 的同时, 主 TaskGroup 仍能跑 mailbox / m4_pump 等
    """
    _running: set[str] = set()

    log.info(
        f"[revising_dispatcher] start, interval={SCAN_INTERVAL_SEC}s, "
        f"claude_timeout={CLAUDE_TIMEOUT_SEC}s"
    )
    while True:
        try:
            rows = await db.read(
                "SELECT id, revise_count FROM drafts WHERE state='revising' "
                "ORDER BY updated_at LIMIT 5"
            )
            for r in rows:
                draft_id = r["id"]
                revise_count = r["revise_count"] or 0

                if draft_id in _running:
                    continue

                # ≥2 轮改稿不再派 Claude, 兜底转 human_queue
                if revise_count >= 2:
                    log.warning(
                        f"[revising] draft {draft_id} revise_count={revise_count} ≥2, "
                        "转 human_queue 不再派工"
                    )
                    await db.transition(
                        draft_id, "revising", "human_queue", "revising_dispatcher",
                        reason=f"revise_count={revise_count} 超 2 轮上限",
                    )
                    continue

                prompt = await _build_revise_prompt(db, draft_id)
                if prompt is None:
                    continue

                _running.add(draft_id)
                # 不 await — 让一个 claude subprocess 跑期间, dispatcher 不阻塞
                # (但同 draft 不会重起, _running 拦着)
                asyncio.create_task(
                    _spawn_and_track(db, draft_id, prompt, _running)
                )
        except Exception as e:
            log.error(f"[revising_dispatcher] scan failed: {e!r}", exc_info=True)
        await asyncio.sleep(SCAN_INTERVAL_SEC)


async def _spawn_and_track(
    db: Database, draft_id: str, prompt: str, _running: set[str]
) -> None:
    """跑 claude 改稿 + 收尾. 不抛, 失败转 human_queue."""
    try:
        result = await run_claude(prompt, timeout_sec=CLAUDE_TIMEOUT_SEC)
        log.info(
            f"[revising] draft {draft_id} claude run_id={result.run_id} "
            f"success={result.success} elapsed={result.elapsed_sec:.1f}s "
            f"timed_out={result.timed_out}"
        )

        if not result.success:
            # claude 失败: 转 human_queue, 留 log
            await db.transition(
                draft_id, "revising", "human_queue", "revising_dispatcher",
                reason=(
                    f"claude subprocess failed (run_id={result.run_id}, "
                    f"exit={result.exit_code}, timed_out={result.timed_out}). "
                    f"log: {result.stdout_path}"
                ),
            )
            log.error(
                f"[revising] draft {draft_id} 改稿失败, 转 human_queue. "
                f"日志 stdout={result.stdout_path} stderr={result.stderr_path}"
            )
            # success 时 Claude 应该已经在工作流里把 draft 推过 send-apv → state pending_human
            # 不再操心, 兜底 watchdog 看 state.
    except Exception as e:
        log.error(
            f"[revising] draft {draft_id} spawn 异常: {e!r}", exc_info=True
        )
        try:
            await db.transition(
                draft_id, "revising", "human_queue", "revising_dispatcher",
                reason=f"spawn 异常: {type(e).__name__}: {str(e)[:120]}",
            )
        except Exception:
            pass
    finally:
        _running.discard(draft_id)
