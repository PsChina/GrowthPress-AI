"""m1 scout_writer agent: web_search 调研 + 撰写 draft + 落盘 + 写库.

入口:
    from growthpress.scout_writer import run
    draft_id, draft = await run("topic 字符串")

流程:
    1. anthropic AsyncClient + web_search server tool 调研
    2. 解析末尾 ```json {...}``` block → DraftSchema
    3. 写 data/drafts/<id>.md (markdown 全文 + sources 列表)
    4. 若传入 db, 写 drafts 表 state='new'
"""
from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from ..core.llm_router import Task, call
from ..db import Database
from .schemas import DraftSchema

log = logging.getLogger("growthpress.scout_writer")

_PROMPT_PATH = Path(__file__).parent / "prompts" / "scout.md"
_PROMPT = _PROMPT_PATH.read_text(encoding="utf-8")

WEB_SEARCH_TOOL: dict[str, Any] = {
    "type": "web_search_20250305",
    "name": "web_search",
    "max_uses": 3,   # 实测 5 次易触发 DeepSeek 端连接超时, 3 次够覆盖 (近 7 天调研)
}

# 匹配末尾 ```json ... ``` block. 用最后一个 (model 偶尔会在中间放示例 block).
_JSON_BLOCK_RE = re.compile(r"```json\s*\n(\{.*?\})\s*\n```", re.DOTALL)


def _extract_full_text(response: Any) -> str:
    """从 anthropic response 提取所有 text block. 跳过 thinking / tool_use / tool_result."""
    parts: list[str] = []
    for block in response.content:
        if getattr(block, "type", None) == "text":
            parts.append(block.text)
    return "".join(parts)


def _parse_draft(full_text: str) -> DraftSchema:
    """从 model 输出里抽出末尾 JSON block + pydantic 校验.

    DeepSeek 偶尔在 body_md (长 markdown) 里没正确 escape 双引号 / 反斜杠,
    严格 JSON 会挂. 用 json5 兜底 — 它允许更宽松语法 (multiline string /
    unescaped quotes), 实战 LLM 输出更稳.
    """
    matches = _JSON_BLOCK_RE.findall(full_text)
    if not matches:
        raise ValueError(
            f"未找到 ```json``` 元数据 block. 输出末尾:\n{full_text[-500:]}"
        )
    raw = matches[-1]  # 取最后一个 block (model 偶尔在中间放示例)

    # 先严格 JSON 试 (快)
    try:
        return DraftSchema.model_validate_json(raw)
    except ValidationError as strict_err:
        # fallback: json5 容错解析
        try:
            import json5
            data = json5.loads(raw)
            return DraftSchema.model_validate(data)
        except Exception as lenient_err:
            raise ValueError(
                f"JSON 解析失败 (strict + json5 都救不了):\n"
                f"  strict: {strict_err}\n"
                f"  json5 : {lenient_err}\n"
                f"原始:\n{raw[:500]}"
            )


def _render_markdown(draft: DraftSchema) -> str:
    """落盘的 markdown 格式. body_md 本身就是 markdown, 拼上 tags / summary / sources."""
    sources_md = "\n".join(f"- [{s.title}]({s.url})" for s in draft.sources) or "_(无)_"
    return (
        f"# {draft.title}\n\n"
        f"**Tags:** {', '.join(draft.tags)}\n\n"
        f"---\n\n"
        f"{draft.body_md}\n\n"
        f"---\n\n"
        f"**Summary:** {draft.summary}\n\n"
        f"## Sources\n\n{sources_md}\n"
    )


async def run(
    topic: str,
    db: Database | None = None,
    *,
    drafts_dir: Path | str = Path("data/drafts"),
) -> tuple[str, DraftSchema]:
    """调研 + 撰写一篇 draft, 落盘 + (可选) 写库.

    Args:
        topic: 主题字符串
        db: 可选 Database 实例. 传入则写 drafts 表 state='new'; 不传仅落盘
        drafts_dir: markdown 落盘目录 (默认 data/drafts/)

    Returns:
        (draft_id, parsed_draft)
    """
    # 走 llm_router 统一调用: 自动按 Task.SCOUT_WRITING 路由 (pro 模型) +
    # 写 llm_calls 表跟踪. 内部已实现 streaming 避免 long-running peer close.
    # draft_id=None: m1 还没生成 draft_id, llm_calls.draft_id 写 NULL 等于"调研类调用".
    log.info(f"[m1] start: topic={topic!r}")
    result = await call(
        task=Task.SCOUT_WRITING,
        system=_PROMPT,
        messages=[{"role": "user", "content": f"主题: {topic}"}],
        tools=[WEB_SEARCH_TOOL],
        max_tokens=6144,
        db=db,
        draft_id=None,
    )
    full_text = result.text
    log.info(
        f"[m1] streamed: text length={len(full_text)} chars, "
        f"model={result.model_used} ({result.model_id}), "
        f"tokens={result.input_tokens}→{result.output_tokens}"
    )

    draft = _parse_draft(full_text)
    log.info(
        f"[m1] parsed: title={draft.title!r}, "
        f"tags={draft.tags}, body={len(draft.body_md)}c, sources={len(draft.sources)}"
    )

    draft_id = uuid.uuid4().hex[:8]
    drafts_dir = Path(drafts_dir)
    drafts_dir.mkdir(parents=True, exist_ok=True)
    md_path = drafts_dir / f"{draft_id}.md"
    md_path.write_text(_render_markdown(draft), encoding="utf-8")
    log.info(f"[m1] saved: {md_path}")

    if db is not None:
        now = datetime.now(timezone.utc).isoformat()
        await db.write(
            """INSERT INTO drafts
               (id, topic, title, body_md, summary, sources, state, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, 'new', ?, ?)""",
            (
                draft_id,
                topic,
                draft.title,
                draft.body_md,
                draft.summary,
                json.dumps([s.model_dump() for s in draft.sources], ensure_ascii=False),
                now,
                now,
            ),
        )
        # 行程守护: 初始 state_log row (from_state=NULL)
        await db.log_initial_state(
            draft_id, to_state="new", by_agent="m1",
            reason=f"topic: {topic[:80]}",
        )
        log.info(f"[m1] db inserted: drafts.id={draft_id} state=new")

    return draft_id, draft
