from __future__ import annotations

import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest

from services.voice_emotion_gate import VoiceEmotionCoordinator
from services.voice_emotion_window import EmotionWindowHost

pytestmark = [pytest.mark.asyncio(loop_scope="function")]


def identity():
    return {
        "session_id": str(uuid4()),
        "input_turn_id": str(uuid4()),
        "input_order": 0,
        "stimulus_revision": 0,
    }


async def test_valid_evaluation_and_hydration_run_in_parallel():
    evaluation_started = asyncio.Event()
    hydration_started = asyncio.Event()

    async def context_reader(value):
        evaluation_started.set()
        return {"identity": {}, "worldview": {}, "goals": {}, "relationships": {}}

    async def evaluator(message, context):
        await hydration_started.wait()
        return {"label": "happy", "logical_call_count": 1}

    async def hydrator(value, message):
        hydration_started.set()
        await evaluation_started.wait()
        return SimpleNamespace(per_response_instructions="context", hydrate_ms=1.0)

    coordinator = VoiceEmotionCoordinator(
        context_reader=context_reader,
        evaluator=evaluator,
        hydrator=hydrator,
        emotion_timeout_ms=1000,
    )
    result = await coordinator.run(identity(), "请继续")

    assert result.released is True
    assert result.context_ready is True
    assert result.visual_window["status"] == "accepted"
    assert result.evaluation["status"] == "valid"
    assert result.hydration.per_response_instructions == "context"
    assert result.metrics["release_reason"] == "evaluation_complete"


async def test_model_timeout_releases_and_late_valid_is_diagnostic_only():
    evaluation_calls = []

    async def context_reader(value):
        return {"identity": {}, "worldview": {}, "goals": {}, "relationships": {}}

    async def evaluator(message, context):
        evaluation_calls.append(message)
        await asyncio.sleep(0.05)
        return {"label": "happy", "logical_call_count": 1}

    async def hydrator(value, message):
        return SimpleNamespace(per_response_instructions="context", hydrate_ms=1.0)

    coordinator = VoiceEmotionCoordinator(
        context_reader=context_reader,
        evaluator=evaluator,
        hydrator=hydrator,
        emotion_timeout_ms=0.001,
    )
    value = identity()
    result = await coordinator.run(value, "请继续")
    await asyncio.sleep(0.06)

    assert result.released is True
    assert result.context_ready is True
    assert result.visual_window["status"] == "closed"
    assert result.visual_window["reason"] == "model_timeout"
    assert result.metrics["release_reason"] == "model_timeout"
    assert coordinator.window_host.current(value)["status"] == "closed"
    assert coordinator.last_evaluation_task.cancelled() is True
    diagnostics = coordinator.window_host.late_diagnostics
    assert [item.status for item in diagnostics] == ["error"]
    assert diagnostics[0].error_type == "CancelledError"
    assert diagnostics[0].accepted is False

    with pytest.raises(RuntimeError, match="already has an evaluation window"):
        await coordinator.run(value, "请继续")
    assert evaluation_calls == ["请继续"]


async def test_ordinary_evaluation_error_releases_without_closing_window():
    hydration_started = asyncio.Event()

    async def context_reader(value):
        return {"identity": {}, "worldview": {}, "goals": {}, "relationships": {}}

    async def evaluator(message, context):
        await hydration_started.wait()
        raise RuntimeError("provider unavailable")

    async def hydrator(value, message):
        hydration_started.set()
        return SimpleNamespace(per_response_instructions="context", hydrate_ms=1.0)

    coordinator = VoiceEmotionCoordinator(
        context_reader=context_reader,
        evaluator=evaluator,
        hydrator=hydrator,
        emotion_timeout_ms=1000,
    )
    value = identity()
    result = await coordinator.run(value, "请继续")

    assert result.released is True
    assert result.context_ready is True
    assert result.visual_window["status"] == "open"
    assert result.evaluation["status"] == "error"
    assert result.metrics["release_reason"] == "evaluation_error"


async def test_duplicate_accepted_identity_fails_loud_without_second_evaluation():
    evaluation_calls = []

    async def context_reader(value):
        return {"identity": {}, "worldview": {}, "goals": {}, "relationships": {}}

    async def evaluator(message, context):
        evaluation_calls.append(message)
        return {"label": "happy", "logical_call_count": 1}

    async def hydrator(value, message):
        return SimpleNamespace(per_response_instructions="context", hydrate_ms=1.0)

    coordinator = VoiceEmotionCoordinator(
        context_reader=context_reader,
        evaluator=evaluator,
        hydrator=hydrator,
        emotion_timeout_ms=1000,
    )
    value = identity()
    await coordinator.run(value, "请继续")

    with pytest.raises(RuntimeError, match="already has an evaluation window"):
        await coordinator.run(value, "请继续")
    assert evaluation_calls == ["请继续"]


class FakeFirstResponse:
    def __init__(self):
        self.called = False

    def consume(self, facts):
        if facts["released"] and facts["context_ready"]:
            self.called = True


async def test_evaluation_release_without_context_does_not_call_first_response():
    async def context_reader(value):
        return {"identity": {}, "worldview": {}, "goals": {}, "relationships": {}}

    async def evaluator(message, context):
        return {"label": "happy", "logical_call_count": 1}

    async def hydrator(value, message):
        raise RuntimeError("hydration unavailable")

    coordinator = VoiceEmotionCoordinator(
        context_reader=context_reader,
        evaluator=evaluator,
        hydrator=hydrator,
        emotion_timeout_ms=1000,
    )
    result = await coordinator.run(identity(), "请继续")
    first_response = FakeFirstResponse()
    first_response.consume(result.to_b_gate_facts())

    assert result.released is True
    assert result.context_ready is False
    assert result.hydration is None
    assert first_response.called is False


async def test_late_hydration_success_still_counts_as_context_ready():
    """水合在评估完成后才成功：context_ready 必须按最终实际结果计算。"""

    async def context_reader(value):
        return {"identity": {}, "worldview": {}, "goals": {}, "relationships": {}}

    async def evaluator(message, context):
        return {"label": "happy", "logical_call_count": 1}

    async def hydrator(value, message):
        await asyncio.sleep(0.02)
        return SimpleNamespace(per_response_instructions="context", hydrate_ms=1.0)

    coordinator = VoiceEmotionCoordinator(
        context_reader=context_reader,
        evaluator=evaluator,
        hydrator=hydrator,
        emotion_timeout_ms=1000,
    )
    result = await coordinator.run(identity(), "请继续")

    assert result.released is True
    assert result.context_ready is True
    assert result.hydration.per_response_instructions == "context"


async def test_hydration_error_keeps_context_not_ready_with_structured_reason():
    """水合失败：context_ready=False，且结构化 reason 说明原因。"""

    async def context_reader(value):
        return {"identity": {}, "worldview": {}, "goals": {}, "relationships": {}}

    async def evaluator(message, context):
        return {"label": "happy", "logical_call_count": 1}

    async def hydrator(value, message):
        raise RuntimeError("hydration unavailable")

    coordinator = VoiceEmotionCoordinator(
        context_reader=context_reader,
        evaluator=evaluator,
        hydrator=hydrator,
        emotion_timeout_ms=1000,
    )
    result = await coordinator.run(identity(), "请继续")

    assert result.released is True
    assert result.context_ready is False
    assert result.hydration is None
    assert result.metrics["hydration_error_type"] == "RuntimeError"


async def test_parent_cancellation_recovers_both_child_tasks_with_diagnostics():
    """父调用取消：结构化回收评估与水合子任务，无 orphan，保留诊断。"""

    started = asyncio.Event()
    cancelled_flags: dict[str, bool] = {"evaluation": False, "hydration": False}

    async def context_reader(value):
        return {"identity": {}, "worldview": {}, "goals": {}, "relationships": {}}

    async def evaluator(message, context):
        started.set()
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancelled_flags["evaluation"] = True
            raise

    async def hydrator(value, message):
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancelled_flags["hydration"] = True
            raise

    coordinator = VoiceEmotionCoordinator(
        context_reader=context_reader,
        evaluator=evaluator,
        hydrator=hydrator,
        emotion_timeout_ms=100_000,
    )
    value = identity()
    run_task = asyncio.create_task(coordinator.run(value, "请继续"))
    await asyncio.wait_for(started.wait(), timeout=0.2)
    await asyncio.sleep(0)

    run_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run_task
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert cancelled_flags == {"evaluation": True, "hydration": True}
    assert coordinator.last_evaluation_task.done()
    diagnostics = coordinator.window_host.late_diagnostics
    assert len(diagnostics) == 1
    assert diagnostics[0].status == "error"
    assert diagnostics[0].error_type == "CancelledError"
    assert diagnostics[0].rejection_reason == "coordinator_cancelled"


async def test_metrics_measure_hydration_and_gate_wait_independently():
    """hydr­ate_ms 覆盖水合自身起止；gate_wait_ms 是三条件齐备时刻。"""

    async def context_reader(value):
        return {"identity": {}, "worldview": {}, "goals": {}, "relationships": {}}

    async def evaluator(message, context):
        await asyncio.sleep(0.02)
        return {"label": "happy", "logical_call_count": 1}

    async def hydrator(value, message):
        await asyncio.sleep(0.04)
        return SimpleNamespace(per_response_instructions="context", hydrate_ms=1.0)

    coordinator = VoiceEmotionCoordinator(
        context_reader=context_reader,
        evaluator=evaluator,
        hydrator=hydrator,
        emotion_timeout_ms=1000,
    )
    result = await coordinator.run(identity(), "请继续")

    assert result.metrics["hydrate_ms"] == pytest.approx(40, abs=15)
    assert result.metrics["gate_wait_ms"] == pytest.approx(40, abs=15)
    assert result.metrics["eval_ms"] == pytest.approx(20, abs=15)
    assert result.metrics["gate_wait_ms"] >= result.metrics["eval_ms"]
    assert result.metrics["gate_wait_ms"] >= result.metrics["hydrate_ms"]


async def test_hydrate_ms_is_not_inflated_when_evaluation_is_slower():
    """评估比水合慢：hydrate_ms 只能反映水合自身耗时，不得吸收评估等待。"""

    async def context_reader(value):
        return {"identity": {}, "worldview": {}, "goals": {}, "relationships": {}}

    async def evaluator(message, context):
        await asyncio.sleep(0.04)
        return {"label": "happy", "logical_call_count": 1}

    async def hydrator(value, message):
        await asyncio.sleep(0.005)
        return SimpleNamespace(per_response_instructions="context", hydrate_ms=1.0)

    coordinator = VoiceEmotionCoordinator(
        context_reader=context_reader,
        evaluator=evaluator,
        hydrator=hydrator,
        emotion_timeout_ms=1000,
    )
    result = await coordinator.run(identity(), "请继续")

    assert result.metrics["hydrate_ms"] == pytest.approx(5, abs=15)
    assert result.metrics["eval_ms"] == pytest.approx(40, abs=15)
    assert result.metrics["gate_wait_ms"] >= 40


async def test_hydration_first_and_model_timeout_closes_visual_window():
    async def context_reader(value):
        return {"identity": {}, "worldview": {}, "goals": {}, "relationships": {}}

    async def evaluator(message, context):
        await asyncio.sleep(0.05)
        return {"label": "happy", "logical_call_count": 1}

    async def hydrator(value, message):
        await asyncio.sleep(0.02)
        return SimpleNamespace(per_response_instructions="context", hydrate_ms=1.0)

    coordinator = VoiceEmotionCoordinator(
        context_reader=context_reader,
        evaluator=evaluator,
        hydrator=hydrator,
        emotion_timeout_ms=0.001,
    )
    result = await coordinator.run(identity(), "请继续")

    assert result.to_b_gate_facts() == {"released": True, "context_ready": True}
    assert result.visual_window["status"] == "closed"
    assert result.visual_window["reason"] == "model_timeout"


async def test_accepted_visual_survives_residual_timer():
    timers = []
    host = EmotionWindowHost(
        now=lambda: 0.0,
        schedule=lambda callback, delay: timers.append((callback, delay))
        or len(timers),
        cancel=lambda handle: None,
    )

    async def context_reader(value):
        return {"identity": {}, "worldview": {}, "goals": {}, "relationships": {}}

    async def evaluator(message, context):
        return {"label": "happy", "logical_call_count": 1}

    async def hydrator(value, message):
        return SimpleNamespace(per_response_instructions="context", hydrate_ms=1.0)

    coordinator = VoiceEmotionCoordinator(
        context_reader=context_reader,
        evaluator=evaluator,
        hydrator=hydrator,
        emotion_timeout_ms=1000,
        window_host=host,
    )
    value = identity()
    result = await coordinator.run(value, "请继续")
    timers[0][0]()

    assert result.visual_window["status"] == "accepted"
    assert host.current(value)["status"] == "accepted"


async def test_old_identity_timeout_does_not_close_new_input():
    async def context_reader(value):
        return {"identity": {}, "worldview": {}, "goals": {}, "relationships": {}}

    async def evaluator(message, context):
        if message == "old":
            await asyncio.sleep(0.05)
        return {"label": "happy", "logical_call_count": 1}

    async def hydrator(value, message):
        return SimpleNamespace(per_response_instructions="context", hydrate_ms=1.0)

    coordinator = VoiceEmotionCoordinator(
        context_reader=context_reader,
        evaluator=evaluator,
        hydrator=hydrator,
        emotion_timeout_ms=1,
    )
    old = identity()
    new = identity()
    old_result = await coordinator.run(old, "old")
    new_result = await coordinator.run(new, "new")

    assert old_result.visual_window["status"] == "closed"
    assert new_result.visual_window["status"] == "accepted"
