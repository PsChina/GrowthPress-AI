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

from ..core import get_llm_client, get_settings
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
    """从 model 输出里抽出末尾 JSON block + pydantic 校验."""
    matches = _JSON_BLOCK_RE.findall(full_text)
    if not matches:
        raise ValueError(
            f"未找到 ```json``` 元数据 block. 输出末尾:\n{full_text[-500:]}"
        )
    raw = matches[-1]  # 取最后一个 block
    try:
        return DraftSchema.model_validate_json(raw)
    except ValidationError as e:
        raise ValueError(f"JSON 元数据 schema 校验失败: {e}\n原始:\n{raw[:500]}")


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
    client = get_llm_client()
    s = get_settings()

    log.info(f"[m1] start: topic={topic!r}, model={s.llm_model}")
    # 用 streaming 而不是 messages.create: long-running (web_search 多次 + 长生成) 时
    # SSE 持续推送 chunk 不会触发 DeepSeek 端 idle 连接关闭 (实测 create 在 60-90s 后
    # 被 peer close, streaming 稳)
    async with client.messages.stream(
        model=s.llm_model,
        max_tokens=6144,
        tools=[WEB_SEARCH_TOOL],
        system=_PROMPT,
        messages=[{"role": "user", "content": f"主题: {topic}"}],
    ) as stream:
        async for _ in stream:
            pass  # 累积过程, 不逐 chunk 处理
        final = await stream.get_final_message()

    full_text = _extract_full_text(final)
    log.info(f"[m1] streamed: text length={len(full_text)} chars")

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
