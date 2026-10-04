"""Closed emotion labels and context selection for the isolated avatar preview."""

import json
from collections.abc import Mapping
from typing import Any

EMOTIONS = frozenset(
    {
        "neutral",
        "happy",
        "excited",
        "curious",
        "surprised",
        "confused",
        "suspicious",
        "angry",
        "sad",
        "scared",
        "proud",
        "shy",
        "bored",
        "playful",
    }
)
CONTEXT_KEYS = ("identity", "worldview", "goals", "relationships")
WORDSET_VERSION = "v1"


def select_context(raw: Any) -> dict[str, Any]:
    """Decode context and return only the four approved categories."""
    value = json.loads(raw) if isinstance(raw, str) else raw
    if not isinstance(value, Mapping) or any(key not in value for key in CONTEXT_KEYS):
        raise ValueError("四类上下文缺失或格式无效；请修复来源或显式选择 fixture")
    return {key: value[key] for key in CONTEXT_KEYS}


def validate_label(content: Any) -> str:
    """Accept only one exact machine label; do not normalize or parse aliases."""
    if not isinstance(content, str) or content not in EMOTIONS:
        raise ValueError("模型未返回一个精确的 v1 情绪标签")
    return content
