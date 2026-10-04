import asyncio
import inspect
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from services import avatar_emotion

FIXTURES = Path(__file__).with_name("fixtures")
BASE_CONTEXT = json.loads((FIXTURES / "context.json").read_text(encoding="utf-8"))
CONFIG = {
    "provider": "openai-chat-completions-endpoint",
    "model": "qwen-plus",
    "endpoint": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "resolved_api_key": "unit-test-secret",
    "timeout_seconds": 1,
}


@pytest.mark.asyncio
async def test_evaluate_sends_only_message_and_four_context_categories(monkeypatch):
    evaluate = getattr(avatar_emotion, "evaluate", None)
    assert callable(evaluate), "Phase 2 must add the independent evaluator"
    captured = {}

    async def fake_call(**kwargs):
        captured.update(kwargs)
        return {"content": "curious", "tool_calls": []}

    raw_context = {
        **BASE_CONTEXT,
        "emotional_state": {"fear": 1},
        "urgent_drives": ["act"],
    }
    result = await evaluate("请解释这段代码", raw_context, CONFIG, call=fake_call)

    assert result["label"] == "curious"
    assert result["logical_call_count"] == 1
    assert result["transport_attempts"] is None
    assert captured["provider"] == CONFIG["provider"]
    assert captured["model"] == CONFIG["model"]
    assert captured["endpoint"] == CONFIG["endpoint"]
    assert captured["api_key"] == CONFIG["resolved_api_key"]
    assert (
        captured["temperature"]
        == inspect.signature(avatar_emotion.llm.chat_completion)
        .parameters["temperature"]
        .default
    )
    assert "max_tokens" not in captured
    assert captured["tools"] is None
    assert captured["response_format"] is None
    assert len(captured["messages"]) == 2
    submitted = json.loads(captured["messages"][1]["content"])
    assert submitted == {"user_message": "请解释这段代码", **BASE_CONTEXT}
    assert set(submitted) == {
        "user_message",
        "identity",
        "worldview",
        "goals",
        "relationships",
    }
    assert captured["messages"][0]["content"] == avatar_emotion.PROMPT_PATH.read_text(
        encoding="utf-8"
    )


@pytest.mark.asyncio
async def test_invalid_business_value_is_rejected_without_resampling():
    evaluate = getattr(avatar_emotion, "evaluate", None)
    assert callable(evaluate), "Phase 2 must add the independent evaluator"
    calls = 0

    async def invalid_once(**_kwargs):
        nonlocal calls
        calls += 1
        return {"content": '{"label":"happy"}', "tool_calls": []}

    with pytest.raises(ValueError, match="精确的 v1"):
        await evaluate("输入", BASE_CONTEXT, CONFIG, call=invalid_once)
    assert calls == 1


@pytest.mark.asyncio
async def test_message_is_not_truncated_by_a_new_character_budget():
    evaluate = getattr(avatar_emotion, "evaluate", None)
    assert callable(evaluate), "Phase 2 must add the independent evaluator"
    message = "x" * 12000
    captured = {}

    async def fake_call(**kwargs):
        captured.update(kwargs)
        return {"content": "neutral", "tool_calls": []}

    await evaluate(message, BASE_CONTEXT, CONFIG, call=fake_call)
    assert json.loads(captured["messages"][1]["content"])["user_message"] == message


@pytest.mark.asyncio
async def test_evaluate_rejects_empty_message_and_tool_calls():
    evaluate = getattr(avatar_emotion, "evaluate", None)
    assert callable(evaluate), "Phase 2 must add the independent evaluator"
    with pytest.raises(ValueError, match="不能为空"):
        await evaluate("   ", BASE_CONTEXT, CONFIG, call=lambda **_kwargs: None)

    async def tool_call(**_kwargs):
        return {"content": "curious", "tool_calls": [{"name": "search"}]}

    with pytest.raises(ValueError, match="工具调用"):
        await evaluate("问题", BASE_CONTEXT, CONFIG, call=tool_call)


@pytest.mark.asyncio
async def test_read_db_context_filters_extras_and_surfaces_read_failure():
    read_db_context = getattr(avatar_emotion, "read_db_context", None)
    assert callable(read_db_context), "Phase 2 must add the DB context reader"

    class Connection:
        async def fetchval(self, query):
            assert query == "SELECT get_appraisal_db_context()"
            return json.dumps({**BASE_CONTEXT, "dopamine_state": {"x": 1}})

    assert await read_db_context(Connection()) == BASE_CONTEXT

    class BrokenConnection:
        async def fetchval(self, _query):
            raise OSError("database unavailable")

    with pytest.raises(OSError, match="database unavailable"):
        await read_db_context(BrokenConnection())


def test_synthetic_semantic_cases_have_predeclared_acceptable_sets():
    cases = json.loads((FIXTURES / "semantic_cases.json").read_text(encoding="utf-8"))
    required = {
        "ordinary_technical_discussion",
        "direct_current_instance_deletion_threat",
        "genuine_unexpected_discovery",
        "affectionate_praise",
        "own_achievement_recognition",
        "quoted_third_party_relationship_harm",
        "relationship_counterfactual",
        "relationship_context_confirmed_safe_cleanup",
        "relationship_context_trust_breach",
    }
    assert {case["id"] for case in cases} == required
    for case in cases:
        assert set(case["context"]) == {
            "identity",
            "worldview",
            "goals",
            "relationships",
        }
        assert case["message"].strip()
        assert case["acceptable_labels"]
        assert set(case["acceptable_labels"]) <= avatar_emotion.EMOTIONS
        assert case["review_reason"].strip()


def test_new_branch_does_not_import_or_expose_legacy_appraisal_effects():
    source = Path(avatar_emotion.__file__).read_text(encoding="utf-8")
    forbidden = (
        "run_subconscious_appraisal",
        "normalize_inline_appraisal",
        "apply_appraisal_drive_effects",
        "apply_appraisal_reward_effects",
    )
    assert not any(name in source for name in forbidden)


@pytest.mark.asyncio
async def test_evaluate_propagates_cancellation_without_retry():
    evaluate = getattr(avatar_emotion, "evaluate", None)
    assert callable(evaluate), "Phase 2 must add the independent evaluator"
    started = asyncio.Event()
    calls = 0

    async def blocked_call(**_kwargs):
        nonlocal calls
        calls += 1
        started.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(
        evaluate("问题", BASE_CONTEXT, CONFIG, call=blocked_call)
    )
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert calls == 1


@pytest.mark.asyncio
async def test_evaluate_timeout_is_one_failed_logical_operation():
    evaluate = getattr(avatar_emotion, "evaluate", None)
    assert callable(evaluate), "Phase 2 must add the independent evaluator"
    calls = 0

    async def blocked_call(**_kwargs):
        nonlocal calls
        calls += 1
        await asyncio.Event().wait()

    config = {**CONFIG, "timeout_seconds": 0.01}
    with pytest.raises(asyncio.TimeoutError):
        await evaluate("问题", BASE_CONTEXT, config, call=blocked_call)
    assert calls == 1


@pytest.mark.asyncio
async def test_host_managed_timeout_branch_does_not_start_a_nested_deadline():
    started = asyncio.Event()

    async def slow_call(**_kwargs):
        started.set()
        await asyncio.Event().wait()
        return {"content": "happy"}

    config = {**CONFIG, "timeout_seconds": 0.01, "host_managed_timeout": True}
    task = asyncio.create_task(
        avatar_emotion.evaluate("问题", BASE_CONTEXT, config, call=slow_call)
    )
    await started.wait()
    await asyncio.sleep(0.02)
    assert not task.done()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
