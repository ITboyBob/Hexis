"""System One 情绪问题转换与 evaluate 分派契约。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from services import avatar_emotion

FIXTURES = Path(__file__).with_name("fixtures")
CONTEXT = json.loads((FIXTURES / "context.json").read_text(encoding="utf-8"))
SYSTEMONE_CONFIG = {
    "provider": "aliyun-systemone",
    "model": "decision-model-preview",
    "endpoint": (
        "https://llm-n5dppxzleuxpia6o.cn-beijing.maas.aliyuncs.com/"
        "compatible-mode/v1/systemone"
    ),
    "resolved_api_key": "unit-test-secret",
    "timeout_seconds": 1,
}
LEGACY_CONFIG = {
    "provider": "openai-chat-completions-endpoint",
    "model": "qwen-plus",
    "endpoint": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "resolved_api_key": "unit-test-secret",
    "timeout_seconds": 1,
}


def test_systemone_question_uses_exact_prompt_definitions_and_closed_wordset():
    question = avatar_emotion.systemone_question()
    prompt = avatar_emotion.PROMPT_PATH.read_text(encoding="utf-8")
    expected = dict(
        line.split("：", 1)
        for line in prompt.splitlines()
        if line.split("：", 1)[0] in avatar_emotion.EMOTIONS
    )

    assert question["type"] == "choice"
    assert question["criteria"] == expected
    assert set(question["criteria"]) == avatar_emotion.EMOTIONS
    assert "输出只能是以下一个精确机器标签" not in question["instructions"]
    assert "只选一项" in question["instructions"]
    assert "neutral" in question["instructions"]


def test_systemone_question_fails_loud_on_prompt_structure_drift(tmp_path, monkeypatch):
    original = avatar_emotion.PROMPT_PATH.read_text(encoding="utf-8")
    cases = {
        "missing-boundary.md": original.replace(
            "输出只能是以下一个精确机器标签", "词表如下"
        ),
        "missing-label.md": original.replace("neutral：未引起明显主导反应。\n", ""),
        "duplicate-label.md": original + "\nneutral：重复定义不允许。\n",
    }
    for name, content in cases.items():
        prompt = tmp_path / name
        prompt.write_text(content, encoding="utf-8")
        monkeypatch.setattr(avatar_emotion, "PROMPT_PATH", prompt)
        with pytest.raises(ValueError, match="词表分界|十四标签"):
            avatar_emotion.systemone_question()


@pytest.mark.asyncio
async def test_systemone_evaluate_sends_state_and_question_without_chat_fields():
    captured = {}

    async def fake_decision(**kwargs):
        captured.update(kwargs)
        return {"content": "curious", "tool_calls": [], "raw": {}}

    result = await avatar_emotion.evaluate(
        "请解释这段代码",
        {**CONTEXT, "emotional_state": {"fear": 1}},
        SYSTEMONE_CONFIG,
        call=fake_decision,
    )

    assert result == {
        "label": "curious",
        "raw_business_value": "curious",
        "wordset_version": "v1",
        "logical_call_count": 1,
        "transport_attempts": None,
    }
    assert captured == {
        "model": "decision-model-preview",
        "endpoint": SYSTEMONE_CONFIG["endpoint"],
        "api_key": "unit-test-secret",
        "state": {"user_message": "请解释这段代码", **CONTEXT},
        "question": avatar_emotion.systemone_question(),
        "timeout_seconds": 1,
    }
    assert not {
        "provider",
        "messages",
        "tools",
        "temperature",
        "response_format",
    }.intersection(captured)


@pytest.mark.asyncio
async def test_legacy_provider_keeps_chat_completion_request_shape():
    captured = {}

    async def fake_chat(**kwargs):
        captured.update(kwargs)
        return {"content": "neutral", "tool_calls": []}

    await avatar_emotion.evaluate(
        "普通问题",
        CONTEXT,
        LEGACY_CONFIG,
        call=fake_chat,
    )

    assert captured["provider"] == LEGACY_CONFIG["provider"]
    assert captured["model"] == "qwen-plus"
    assert len(captured["messages"]) == 2
    assert "state" not in captured
    assert "question" not in captured


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "config",
    [
        {**LEGACY_CONFIG, "model": "decision-model-preview"},
        {**SYSTEMONE_CONFIG, "model": "qwen-plus"},
    ],
)
async def test_decision_model_and_provider_must_be_selected_together(config):
    calls = 0

    async def should_not_call(**_kwargs):
        nonlocal calls
        calls += 1
        return {"content": "neutral", "tool_calls": []}

    with pytest.raises(ValueError, match="decision-model-preview|aliyun-systemone"):
        await avatar_emotion.evaluate(
            "问题",
            CONTEXT,
            config,
            call=should_not_call,
        )
    assert calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("label", sorted(avatar_emotion.EMOTIONS))
async def test_all_closed_labels_keep_the_five_field_contract(label):
    calls = 0

    async def fake_decision(**_kwargs):
        nonlocal calls
        calls += 1
        return {"content": label, "tool_calls": [], "raw": {}}

    result = await avatar_emotion.evaluate(
        "问题",
        CONTEXT,
        SYSTEMONE_CONFIG,
        call=fake_decision,
    )
    assert result == {
        "label": label,
        "raw_business_value": label,
        "wordset_version": "v1",
        "logical_call_count": 1,
        "transport_attempts": None,
    }
    assert calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("content", ["Happy", " happy", "happy ", '{"label":"happy"}'])
async def test_invalid_systemone_labels_are_not_normalized_or_resampled(content):
    calls = 0

    async def invalid(**_kwargs):
        nonlocal calls
        calls += 1
        return {"content": content, "tool_calls": [], "raw": {}}

    with pytest.raises(avatar_emotion.InvalidEmotionLabel):
        await avatar_emotion.evaluate(
            "问题",
            CONTEXT,
            SYSTEMONE_CONFIG,
            call=invalid,
        )
    assert calls == 1
