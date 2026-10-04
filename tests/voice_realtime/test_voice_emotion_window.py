from __future__ import annotations

from uuid import uuid4

from services.avatar_emotion import InvalidEmotionLabel
from services.voice_emotion_window import EmotionWindowHost, TimeoutKind


def identity():
    return {
        "session_id": str(uuid4()),
        "input_turn_id": str(uuid4()),
        "input_order": 0,
        "stimulus_revision": 0,
    }


def test_open_accept_cancels_timer_and_late_timer_is_noop():
    now = [0.0]
    timers = []
    host = EmotionWindowHost(
        now=lambda: now[0],
        schedule=lambda callback, delay: timers.append((callback, delay))
        or len(timers),
        cancel=lambda handle: None,
    )
    value = identity()
    assert host.open(value, 100)["status"] == "open"
    accepted = host.accept_result(value)
    assert accepted["accepted"] is True
    assert host.current(value)["status"] == "accepted"
    timers[0][0]()
    assert host.current(value)["status"] == "accepted"


def test_deadline_first_closes_and_late_result_is_diagnostic_only():
    now = [0.0]
    host = EmotionWindowHost(now=lambda: now[0])
    value = identity()
    host.open(value, 100)
    now[0] = 100
    expired = host.expire(value)
    assert expired["status"] == "closed"
    assert expired["reason"] == "model_timeout"
    late = host.accept_result(value)
    assert late["accepted"] is False
    assert late["fact"]["status"] == "closed"


def test_old_identity_timeout_does_not_close_new_input():
    host = EmotionWindowHost(now=lambda: 0.0)
    old = identity()
    new = identity()
    host.open(old, 100)
    host.expire(old)
    assert host.current(new) is None
    opened = host.open(new, 100)
    assert opened["status"] == "open"


def test_timeout_kinds_have_distinct_structured_reasons():
    host = EmotionWindowHost(now=lambda: 0.0)
    model_value = identity()
    total_value = identity()
    host.open(model_value, 100)
    host.open(total_value, 100)

    model_closed = host.close_for_timeout(model_value, TimeoutKind.MODEL_TIMEOUT)
    total_closed = host.close_for_timeout(total_value, TimeoutKind.TOTAL_DEADLINE)

    assert model_closed["reason"] == "model_timeout"
    assert total_closed["reason"] == "total_deadline"


def test_late_results_are_diagnostics_and_do_not_update_visuals():
    now = [0.0]
    host = EmotionWindowHost(now=lambda: now[0])
    value = identity()
    host.open(value, 100)
    now[0] = 100
    host.expire(value)

    host.record_late_result(value, {"label": "happy"}, status="valid")
    host.record_late_result(value, None, status="pending")
    host.record_late_result(value, {"status": "closed"}, status="query")
    host.record_late_result(value, {"label": "happy"}, status="valid")

    assert host.current(value)["status"] == "closed"
    assert [item.status for item in host.late_diagnostics] == [
        "valid",
        "pending",
        "query",
        "valid",
    ]
    assert all(item.accepted is False for item in host.late_diagnostics)


def test_invalid_label_and_context_errors_do_not_close_window():
    host = EmotionWindowHost(now=lambda: 0.0)
    value = identity()
    host.open(value, 100)

    host.record_late_error(value, InvalidEmotionLabel("not-a-label"))
    for stage in ("context_connect", "context_read", "context_close"):
        host.record_late_error(value, RuntimeError(stage))

    assert host.current(value)["status"] == "open"


def test_transport_retries_remain_one_logical_call():
    host = EmotionWindowHost(now=lambda: 0.0)
    value = identity()
    host.open(value, 100)
    host.record_transport_attempts(value, 3)

    metrics = host.call_metrics(value)
    assert metrics["logical_call_count"] == 1
    assert metrics["transport_attempts"] == 3
