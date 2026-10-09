"""Closed emotion labels and context selection for the isolated avatar preview."""

import asyncio
import inspect
import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from core import llm

PROMPT_PATH = Path(__file__).with_name("prompts") / "avatar_emotion.md"
PROMPT_VERSION = "v1"

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


class InvalidEmotionLabel(ValueError):
    """An exact machine response that does not match the closed emotion set."""

    def __init__(self, raw_business_value: Any) -> None:
        super().__init__("模型业务内容不是一个精确的 v1 情绪标签")
        self.raw_business_value = raw_business_value


def select_context(raw: Any) -> dict[str, Any]:
    """Decode context and return only the four approved categories."""
    value = json.loads(raw) if isinstance(raw, str) else raw
    if not isinstance(value, Mapping) or any(key not in value for key in CONTEXT_KEYS):
        raise ValueError("四类上下文缺失或格式无效；请修复来源或显式选择 fixture")
    return {key: value[key] for key in CONTEXT_KEYS}


def validate_label(content: Any) -> str:
    """Accept only one exact machine label; do not normalize or parse aliases."""
    if not isinstance(content, str) or content not in EMOTIONS:
        raise InvalidEmotionLabel(content)
    return content


def systemone_question() -> dict[str, Any]:
    """Convert the single prompt source into one TypeSafe choice question."""
    text = PROMPT_PATH.read_text(encoding="utf-8")
    instructions, separator, _ = text.partition("输出只能是以下一个精确机器标签")
    if not separator:
        raise ValueError("情绪提示词缺少词表分界")
    rows = re.findall(r"^([a-z]+)：(.+)$", text, flags=re.MULTILINE)
    criteria = dict(rows)
    if len(rows) != len(criteria) or set(criteria) != EMOTIONS:
        raise ValueError("情绪提示词定义与十四标签不一致")
    return {
        "type": "choice",
        "instructions": instructions.strip()
        + "\n只选一项；没有明显主导反应可选 neutral。",
        "criteria": criteria,
    }


async def read_db_context(conn: Any) -> dict[str, Any]:
    """Read the existing appraisal context and retain only the approved fields."""
    raw = await conn.fetchval("SELECT get_appraisal_db_context()")
    value = json.loads(raw) if isinstance(raw, str) else raw
    if not isinstance(value, Mapping):
        raise ValueError("四类上下文缺失或格式无效；请修复来源或显式选择 fixture")
    normalized = {key: value[key] if key in value else [] for key in CONTEXT_KEYS}
    return select_context(normalized)


async def evaluate(
    message: str,
    context: Any,
    config: Mapping[str, Any],
    *,
    call: Any | None = None,
) -> dict[str, Any]:
    """Run one isolated logical emotion evaluation through the shared LLM transport."""
    if not isinstance(message, str) or not message.strip():
        raise ValueError("消息不能为空")

    selected_context = select_context(context)
    if config["provider"] == "aliyun-systemone":
        if config["model"] != "decision-model-preview":
            raise ValueError("aliyun-systemone 必须显式选择 decision-model-preview")
        from core.providers.aliyun_decision import decision_completion

        completion = call or decision_completion
        request = completion(
            model=config["model"],
            endpoint=config["endpoint"],
            api_key=config["resolved_api_key"],
            state={"user_message": message, **selected_context},
            question=systemone_question(),
            timeout_seconds=config["timeout_seconds"],
        )
    else:
        if config["model"] == "decision-model-preview":
            raise ValueError("decision-model-preview 必须显式选择 aliyun-systemone")
        user_content = json.dumps(
            {"user_message": message, **selected_context}, ensure_ascii=False
        )
        messages = [
            {
                "role": "system",
                "content": PROMPT_PATH.read_text(encoding="utf-8"),
            },
            {"role": "user", "content": user_content},
        ]
        completion = call or llm.chat_completion
        temperature = (
            inspect.signature(llm.chat_completion).parameters["temperature"].default
        )
        request = completion(
            provider=config["provider"],
            model=config["model"],
            endpoint=config["endpoint"],
            api_key=config["resolved_api_key"],
            messages=messages,
            tools=None,
            temperature=temperature,
            response_format=None,
        )
    if config.get("host_managed_timeout"):
        response = await request
    else:
        response = await asyncio.wait_for(request, timeout=config["timeout_seconds"])
    if response.get("tool_calls"):
        raise ValueError("情绪输出不能包含工具调用")
    content = response.get("content")
    return {
        "label": validate_label(content),
        "raw_business_value": content,
        "wordset_version": WORDSET_VERSION,
        "logical_call_count": 1,
        "transport_attempts": None,
    }
