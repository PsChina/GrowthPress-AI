"""daemon task: 扫 email_intake.state='pending' → 起 claude subprocess 跑 chat 任务.

闭环:
  用户邮件 (subject 含"发"/"发一篇") → m5 dispatcher → handle_email_chat
  INSERT email_intake state='pending' intent='post'
  → 本 dispatcher 60s 内扫到 → CAS 锁 state='processing' → 起 claude subprocess
  → claude 跑 growthpress-post skill (search/找图/draft new --state approved)
  → m3_pump 60s 内扫到 approved → SMTP 发 APV 邮件给用户
  → 用户回"批准" → m5 → m3 → m4 真发 → PUB 邮件

claude 成功 (exit 0) → state='done'. 失败 → state='failed', 留 error 文本.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from ..db import Database
from .claude_subprocess import DEFAULT_TIMEOUT_SEC, run_claude

log = logging.getLogger("growthpress.runners.email_chat")

SCAN_INTERVAL_SEC = 60
CLAUDE_TIMEOUT_SEC = DEFAULT_TIMEOUT_SEC   # 30 min, 含 WebSearch + 下载 + 写文


def _build_post_prompt(*, intake_id: str, sender: str, subject: str, body_text: str) -> str:
    """拼 claude 发布任务 prompt. claude 在 GrowthPress-AI cwd 跑, 能用 .claude/skills."""
    return f"""你是 GrowthPress AI daemon 起的 claude subprocess. 用户 {sender!r} 通过邮件给你
派了一个发布任务. 完整邮件信息:

  Subject: {subject}
  Body:
  {body_text}

任务: 根据用户邮件内容判断他想发什么主题到哪个平台, 然后跑 GrowthPress
发布工作流 (`.claude/skills/growthpress-post/SKILL.md` 是完整说明, 必读).

完整工作流 (每一步必做, 不能跳过最后一步):
  1. 从邮件主题/正文提炼"主题"和"目标平台"(默认小红书 image_note)
  2. WebSearch + WebFetch 找含图 CC0 来源 (Unsplash / Pexels / Pixabay)
  3. curl 下载 1-3 张图到 data/test-images/{intake_id}/
  4. 写一篇适合目标平台调性的图文笔记 (小红书: 标题 ≤20 字, 正文 ≤1000 字,
     含 emoji, 友好松弛的口吻)
  5. **⚠ 必须执行**: 调 `python -m growthpress draft new --title "..." --body "..." --state approved --media data/test-images/{intake_id}/0.jpg --media .../1.jpg --media .../2.jpg`
     创建 draft 入库. **不调这条命令任务就算失败** — 图下载了没用, 用户收不到 APV 邮件.

⚠ 关键: 上面第 5 步是 daemon 判定成功的硬指标. 你写完文之后必须真的调这条 CLI
命令把 draft 落 DB. 不要只在回复里"展示"内容然后退出 — 那样 daemon 拿不到 draft,
用户什么都看不到. 验证方式: 调完命令后 `python -m growthpress draft list --limit 1`
应该看到刚建的 draft.

注意:
  - 你 **不要** 直接调 publisher 真发, 也 **不要** 用 --no-dry-run. 真发由 daemon
    在用户回邮件 "批准" 后通过 m4 自动触发.
  - 图源 **只能** CC0 (Unsplash / Pexels / Pixabay) 或用户邮件附带的链接. 普通网页
    抓图违反版权 + 真人肖像权.
  - 你跑完后, daemon 通过你的 exit code 判断成败. exit 0 = 成功. 失败留 error log
    给我看. 别静默退出.
  - 这条 email_intake 记录 id={intake_id}, 你 **不需要** 手动更新它的 state,
    dispatcher 会根据你的 exit code 自动转 done/failed.
"""


async def email_chat_dispatcher_run(db: Database) -> None:
    """长生命周期 task. 60s 扫 email_intake state='pending'.

    串行起 claude (一次一个 chat 任务) — 与 revising_dispatcher 同 quota 池,
    不并发 claude 避免撞 OAuth rate limit.
    """
    _running: set[str] = set()

    log.info(
        f"[email_chat_dispatcher] start, interval={SCAN_INTERVAL_SEC}s, "
        f"claude_timeout={CLAUDE_TIMEOUT_SEC}s"
    )
    while True:
        try:
            rows = await db.read(
                "SELECT id, sender, subject, body_text, intent FROM email_intake "
                "WHERE state='pending' ORDER BY created_at LIMIT 5"
            )
            for r in rows:
                intake_id = r["id"]

                if intake_id in _running:
                    continue

                # MVP: 只跑 intent='post', 其他 (chat / unknown) 暂跳过
                if r["intent"] != "post":
                    log.info(
                        f"[email_chat] intake {intake_id} intent={r['intent']!r} "
                        "MVP 不处理, 跳过"
                    )
                    continue

                # CAS 锁: pending → processing. 失败说明被并发占走, 跳过.
                locked = await _cas_intake_state(db, intake_id, "pending", "processing")
                if not locked:
                    continue

                prompt = _build_post_prompt(
                    intake_id=intake_id,
                    sender=r["sender"] or "",
                    subject=r["subject"] or "",
                    body_text=r["body_text"] or "",
                )

                _running.add(intake_id)
                asyncio.create_task(
                    _spawn_and_track(db, intake_id, prompt, _running)
                )
        except Exception as e:
            log.error(f"[email_chat_dispatcher] scan failed: {e!r}", exc_info=True)
        await asyncio.sleep(SCAN_INTERVAL_SEC)


async def _cas_intake_state(
    db: Database, intake_id: str, from_state: str, to_state: str
) -> bool:
    """email_intake CAS state (drafts.transition 同样模式, 但 email_intake 不在 state_log).

    UPDATE rowcount=1 表示锁住, 0 表示已被改过.
    """
    now = datetime.now(timezone.utc).isoformat()
    # 经过 writer queue 的 write 不返 rowcount, 用底层 _exec 兜.
    result = await db._exec(
        "UPDATE email_intake SET state=?, updated_at=? WHERE id=? AND state=?",
        (to_state, now, intake_id, from_state),
    )
    return result["rowcount"] == 1


async def _spawn_and_track(
    db: Database, intake_id: str, prompt: str, _running: set[str]
) -> None:
    """跑 claude + 落 state. 不抛, 失败标 failed."""
    try:
        result = await run_claude(prompt, timeout_sec=CLAUDE_TIMEOUT_SEC)
        now = datetime.now(timezone.utc).isoformat()

        log.info(
            f"[email_chat] intake {intake_id} claude run_id={result.run_id} "
            f"success={result.success} elapsed={result.elapsed_sec:.1f}s "
            f"timed_out={result.timed_out}"
        )

        if result.success:
            await db.write(
                "UPDATE email_intake SET state='done', claude_run_id=?, updated_at=? "
                "WHERE id=?",
                (result.run_id, now, intake_id),
            )
        else:
            err = (
                f"exit={result.exit_code}, timed_out={result.timed_out}, "
                f"log: {result.stderr_path}"
            )
            await db.write(
                "UPDATE email_intake SET state='failed', claude_run_id=?, error=?, "
                "updated_at=? WHERE id=?",
                (result.run_id, err[:500], now, intake_id),
            )
            log.error(
                f"[email_chat] intake {intake_id} claude 失败. "
                f"stdout={result.stdout_path} stderr={result.stderr_path}"
            )
    except Exception as e:
        log.error(
            f"[email_chat] intake {intake_id} spawn 异常: {e!r}", exc_info=True
        )
        now = datetime.now(timezone.utc).isoformat()
        try:
            await db.write(
                "UPDATE email_intake SET state='failed', error=?, updated_at=? "
                "WHERE id=?",
                (f"spawn 异常: {type(e).__name__}: {str(e)[:200]}", now, intake_id),
            )
        except Exception as e2:
            log.error(f"[email_chat] failed-mark 也失败: {e2!r}")
    finally:
        _running.discard(intake_id)


__all__ = ["email_chat_dispatcher_run"]
