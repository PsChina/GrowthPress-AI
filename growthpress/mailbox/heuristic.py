"""m5 启发式判别 — 区分"用户喂选题" vs "用户喂完整文章".

参 memory project_growthpress_human_loop.md L50-L98 伪代码:
- (a) 含 INTENT_KEYWORDS_DIRECT (直接发/不审/原文/as is/就这样发) 或
       INTENT_KEYWORDS_REVIEW (审一下/AI 看看再发/质量审查) → True
- (b) 长度 > 200 字 + 含 `^#{1,3}\\s` markdown header → True
- (c) LLM fallback V1 不实装 → False (保守)

阈值倾向**保守**: 不确定就 return False, 走 email_topic 安全 (m1 重新生成,
保留人工兜底); 偏"过度走 m1"安全, 偏"跳过 m1 误发"危险.
"""
from __future__ import annotations

import logging
import re

from .schemas import PublishIntent

log = logging.getLogger("growthpress.mailbox.heuristic")

# 明确"直接发"语义关键词 → skip_m2_quality + skip_m3 (但 m2 合规仍强制)
INTENT_KEYWORDS_DIRECT: tuple[str, ...] = (
    "直接发", "不审", "原文发布", "原文发", "as is", "就这样发",
)

# 明确"审一下再发"语义关键词 → 走完整流程
INTENT_KEYWORDS_REVIEW: tuple[str, ...] = (
    "审一下", "AI 看看再发", "AI看看再发", "质量审查", "审完再发",
)

# 200 字阈值 (memory 伪代码) + markdown header 检测
_MIN_ARTICLE_LEN = 200
_RE_MD_HEADER = re.compile(r"^#{1,3}\s", re.MULTILINE)


def _has_any(body: str, keywords: tuple[str, ...]) -> bool:
    """大小写不敏感包含检查."""
    body_lower = body.lower()
    return any(k.lower() in body_lower for k in keywords)


def looks_like_complete_article(body: str) -> bool:
    """启发式: 满足任一即视为"完整文章".

    保守阈值 — V1 不接 LLM fallback, 不确定就 False (走 email_topic).
    """
    if not body:
        return False

    # (a) 明确指令关键词 (DIRECT 或 REVIEW 都视为"用户已经写好/带文章")
    if _has_any(body, INTENT_KEYWORDS_DIRECT) or _has_any(body, INTENT_KEYWORDS_REVIEW):
        log.debug("[heuristic] hit intent keyword → complete_article=True")
        return True

    # (b) 长度 + markdown header
    if len(body) > _MIN_ARTICLE_LEN and _RE_MD_HEADER.search(body):
        log.debug(
            f"[heuristic] len={len(body)} > {_MIN_ARTICLE_LEN} + md header → "
            f"complete_article=True"
        )
        return True

    # (c) LLM fallback V1 不实装. 保守: False → 走 email_topic.
    return False


def parse_publish_intent(body: str) -> PublishIntent:
    """从正文提取发布意图. 决定 email_content handler 跳哪几个模块.

    DIRECT 关键词 → 跳 m2 质量 + 跳 m3 APV (m2 合规仍强制不能 skip, 在 handler 实施)
    REVIEW 关键词 → 走完整 (skip_*=False)
    默认保守 → 走完整
    """
    if not body:
        return PublishIntent()

    if _has_any(body, INTENT_KEYWORDS_DIRECT):
        return PublishIntent(skip_m2_quality=True, skip_m3=True)
    if _has_any(body, INTENT_KEYWORDS_REVIEW):
        return PublishIntent(skip_m2_quality=False, skip_m3=False)
    return PublishIntent()


__all__ = [
    "INTENT_KEYWORDS_DIRECT",
    "INTENT_KEYWORDS_REVIEW",
    "looks_like_complete_article",
    "parse_publish_intent",
]
