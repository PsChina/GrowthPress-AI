"""m2 reviewer agent: 三路审核 (合规 / 质量 / 平台) + state 变迁.

流程:
  1. 锁 state: new → reviewing (db.transition CAS)
  2. 合规短路: critical 直接 human_queue, 不浪费 quality/platform LLM 调用
  3. 质量 + 平台 并行 (TaskGroup)
  4. 落 reviews 表 + state 决定:
     - 全过 → approved
     - 不过 + revise_count < 2 → revising
     - 不过 + revise_count ≥ 2 → human_queue
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from ..core.llm_router import Task, call
from ..db import Database
from .schemas import (
    ComplianceResult,
    PlatformResult,
    QualityResult,
    Verdict,
)

log = logging.getLogger("growthpress.reviewer")

_PROMPT_DIR = Path(__file__).parent / "prompts"
_PROMPT_COMPLIANCE = (_PROMPT_DIR / "compliance.md").read_text(encoding="utf-8")
_PROMPT_QUALITY    = (_PROMPT_DIR / "quality.md").read_text(encoding="utf-8")
_PROMPT_PLATFORM   = (_PROMPT_DIR / "platform.md").read_text(encoding="utf-8")

_FENCE_RE = re.compile(r"```(?:json)?\s*\n(\{.*?\})\s*\n```", re.DOTALL)


def _parse_json(text: str) -> dict:
    """提取 JSON: 优先 fenced block, 失败再尝试整段 text. parse 失败抛 ValueError."""
    if not text:
        raise ValueError("LLM 输出为空")
    matches = _FENCE_RE.findall(text)
    raw = matches[-1] if matches else text.strip()
    # 如果整段是 JSON 但被前后无关字符包裹, 找第一个 { 到最后一个 }
    if not raw.startswith("{"):
        i, j = raw.find("{"), raw.rfind("}")
        if i >= 0 and j > i:
            raw = raw[i : j + 1]
    try:
        return json.loads(raw)
    except json.JSONDecodeError as strict_err:
        # LLM 偶尔 issues/suggested_edits 长 string 没 escape 双引号 / 反斜杠,
        # json5 容错处理 (multiline string / unescaped quotes / 单引号 都 OK)
        try:
            import json5
            return json5.loads(raw)
        except Exception as lenient_err:
            raise ValueError(
                f"JSON parse fail (json + json5 都救不了):\n"
                f"  strict: {strict_err}\n"
                f"  json5 : {lenient_err}\n"
                f"截取:\n{raw[:300]}"
            )


_BODY_EXCERPT_LIMIT = 2000   # flash 模型对长输入不稳, m2 看摘要 + 前 2000 字够判


def _format_draft(draft: dict[str, Any]) -> str:
    """把 draft 行格式化成 LLM user_msg. 截断 body 防 flash 模型返回空."""
    body = draft.get("body_md") or ""
    body_excerpt = body[:_BODY_EXCERPT_LIMIT]
    if len(body) > _BODY_EXCERPT_LIMIT:
        body_excerpt += f"\n\n... (正文共 {len(body)} 字, 此处截断)"
    sources = (draft.get("sources") or "[]")[:600]
    return (
        f"# 标题\n{draft.get('title', '')}\n\n"
        f"# 摘要\n{draft.get('summary', '')}\n\n"
        f"# 正文 (前 {_BODY_EXCERPT_LIMIT} 字)\n{body_excerpt}\n\n"
        f"# Sources\n{sources}"
    )


async def _call_with_fallback(task: Task, **kwargs):
    """LLM 返回空 text 时用 pro 重试一次 (简易升级 hook).

    flash 偶尔对长 prompt 返回 0 字, 升级 pro 更稳. 未来 llm_router 内置升级时
    本函数可拆掉.
    """
    result = await call(task=task, **kwargs)
    if not result.text.strip():
        log.warning(f"[m2] {task.value} 返回空 text, 升级 pro 重试")
        kwargs.pop("_force_model", None)
        result = await call(task=task, _force_model="pro", **kwargs)
    return result


async def check_compliance(
    draft: dict, *, db: Database | None, draft_id: str
) -> ComplianceResult:
    """整体 try/except 兜底, LLM/parse 失败回 critical 不挂 TaskGroup."""
    try:
        result = await _call_with_fallback(
            Task.REVIEW_COMPLIANCE,
            system=_PROMPT_COMPLIANCE,
            messages=[{"role": "user", "content": _format_draft(draft)}],
            max_tokens=512,
            db=db,
            draft_id=draft_id,
        )
        data = _parse_json(result.text)
        return ComplianceResult.model_validate(data)
    except Exception as e:
        log.warning(f"[m2] compliance 失败 ({type(e).__name__}): {e}, fallback critical")
        return ComplianceResult(
            passed=False, severity="critical",
            issues=[f"AI 调用/解析异常: {type(e).__name__}: {str(e)[:150]}"],
        )


async def check_quality(
    draft: dict, *, db: Database | None, draft_id: str
) -> QualityResult:
    try:
        result = await _call_with_fallback(
            Task.REVIEW_QUALITY,
            system=_PROMPT_QUALITY,
            messages=[{"role": "user", "content": _format_draft(draft)}],
            max_tokens=2048,    # issues + suggested_edits 各 5+ 条长 string 时 1024 易截
            db=db,
            draft_id=draft_id,
        )
        data = _parse_json(result.text)
        return QualityResult.model_validate(data)
    except Exception as e:
        log.warning(f"[m2] quality 失败 ({type(e).__name__}): {e}, fallback score=0")
        return QualityResult(
            passed=False, score=0,
            issues=[f"AI 调用/解析异常: {type(e).__name__}: {str(e)[:150]}"],
        )


async def check_platforms(
    draft: dict, *, db: Database | None, draft_id: str
) -> PlatformResult:
    try:
        result = await _call_with_fallback(
            Task.REVIEW_PLATFORM,
            system=_PROMPT_PLATFORM,
            messages=[{"role": "user", "content": _format_draft(draft)}],
            max_tokens=2048,   # 4 平台 × 多条 issues, 1024 易被截 (sanity 撞过)
            db=db,
            draft_id=draft_id,
        )
        data = _parse_json(result.text)
        return PlatformResult.model_validate(data)
    except Exception as e:
        log.warning(f"[m2] platform 失败 ({type(e).__name__}): {e}, fallback pass+warn")
        return PlatformResult(
            passed=True,
            issues_by_platform={"_call_error": [str(e)[:150]]},
        )


async def _save_review(
    db: Database,
    draft_id: str,
    round_: int,
    compliance: ComplianceResult,
    quality: QualityResult | None,
    platform: PlatformResult | None,
) -> None:
    """落 reviews 表 (一行总结 + JSON 字段存细节)."""
    passed = compliance.passed and (
        quality is None or quality.passed
    ) and (
        platform is None or platform.passed
    )
    issues: list[str] = list(compliance.issues)
    if quality:
        issues.extend(quality.issues)
    if platform:
        for plat_issues in platform.issues_by_platform.values():
            issues.extend(plat_issues)
    suggested_edits: list[str] = []
    if quality:
        suggested_edits.extend(quality.suggested_edits)
    if platform:
        suggested_edits.extend(platform.suggested_edits)

    await db.write(
        """INSERT INTO reviews
           (draft_id, round, pass_, score, issues, suggested_edits, reviewed_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            draft_id,
            round_,
            passed,
            quality.score if quality else None,
            json.dumps(issues, ensure_ascii=False),
            json.dumps(suggested_edits, ensure_ascii=False),
            datetime.now(timezone.utc).isoformat(),
        ),
    )


async def review(
    draft_id: str, db: Database, *, round_: int = 1
) -> Verdict | None:
    """主入口. 返回 Verdict, 同时落 reviews 表 + 改 drafts.state.

    None 返回情形: draft 不存在 / 不在 new state (可能被别的 task 抢走).
    """
    rows = await db.read("SELECT * FROM drafts WHERE id=?", (draft_id,))
    if not rows:
        log.error(f"[m2] draft {draft_id!r} 不存在")
        return None
    draft = dict(rows[0])

    if not await db.transition(draft_id, "new", "reviewing", "m2", reason=f"round={round_}"):
        log.warning(f"[m2] {draft_id!r} 不在 new state (当前={draft['state']!r}), 跳过")
        return None

    log.info(f"[m2] start review draft={draft_id} round={round_} title={draft.get('title')!r}")

    # 1. 合规短路
    compliance = await check_compliance(draft, db=db, draft_id=draft_id)
    log.info(f"[m2] compliance: passed={compliance.passed} severity={compliance.severity}")
    if not compliance.passed:
        await _save_review(db, draft_id, round_, compliance, None, None)
        await db.transition(
            draft_id, "reviewing", "human_queue", "m2",
            reason=f"compliance {compliance.severity}: {compliance.issues[:3]}",
        )
        return Verdict(
            passed=False, compliance=compliance,
            quality=None, platform=None,
            all_issues=compliance.issues,
        )

    # 2. 质量 + 平台 并行
    async with asyncio.TaskGroup() as tg:
        quality_t = tg.create_task(check_quality(draft, db=db, draft_id=draft_id))
        platform_t = tg.create_task(check_platforms(draft, db=db, draft_id=draft_id))
    quality = quality_t.result()
    platform = platform_t.result()
    log.info(
        f"[m2] quality: passed={quality.passed} score={quality.score} "
        f"platform: passed={platform.passed}"
    )

    # 3. 落 reviews + 决定 state
    await _save_review(db, draft_id, round_, compliance, quality, platform)

    all_issues = list(compliance.issues) + quality.issues
    for plat_issues in platform.issues_by_platform.values():
        all_issues.extend(plat_issues)

    overall_pass = compliance.passed and quality.passed and platform.passed
    if overall_pass:
        await db.transition(
            draft_id, "reviewing", "approved", "m2",
            reason=f"score={quality.score}",
        )
    elif draft.get("revise_count", 0) >= 2:
        await db.transition(
            draft_id, "reviewing", "human_queue", "m2",
            reason=f"超 2 轮 revise (round={round_})",
        )
    else:
        await db.transition(
            draft_id, "reviewing", "revising", "m2",
            reason=f"issues: {all_issues[:3]}",
        )

    return Verdict(
        passed=overall_pass,
        compliance=compliance,
        quality=quality,
        platform=platform,
        all_issues=all_issues,
    )
