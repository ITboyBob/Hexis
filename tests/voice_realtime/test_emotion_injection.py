from __future__ import annotations

from types import SimpleNamespace

import pytest

from services.avatar_emotion import EMOTIONS
from services.voice_instructions import eligible_gate_emotion, render_emotion


IDENTITY = {
    "session_id": "session-1",
    "input_turn_id": "input-1",
    "input_order": 4,
    "stimulus_revision": 0,
}


def _gate(*, identity=None, evaluation=None, released=True, ready=True, window=None):
    return SimpleNamespace(
        identity=dict(identity or IDENTITY),
        released=released,
        context_ready=ready,
        evaluation=evaluation or {
            "status": "valid",
            "accepted": True,
            "value": {"label": "happy"},
        },
        visual_window=window or {"status": "accepted", "owner": "service"},
    )


@pytest.mark.parametrize("label", sorted(EMOTIONS))
def test_every_closed_v1_label_can_be_injected(label):
    assert render_emotion(label, eligible=True) == f"## Emotion\n{label}"


@pytest.mark.parametrize("value", ["Happy", " happy", "happy ", "joyful", "happy!", "happy because", None, 3])
def test_invalid_labels_are_omitted_without_neutral_fallback(value):
    assert render_emotion(value, eligible=True) == ""


def test_missing_error_and_late_evaluation_are_ineligible():
    for evaluation in (
        {"status": "missing"},
        {"status": "error", "error_type": "RuntimeError"},
        {"status": "timeout"},
        {"status": "valid", "accepted": False, "value": {"label": "happy"}},
    ):
        assert eligible_gate_emotion(_gate(evaluation=evaluation), IDENTITY) == (None, False)


@pytest.mark.parametrize("field,value", [("session_id", "old"), ("input_turn_id", "old"), ("input_order", 3), ("stimulus_revision", 1)])
def test_stale_identity_is_ineligible(field, value):
    stale = {**IDENTITY, field: value}
    assert eligible_gate_emotion(_gate(), stale) == (None, False)


def test_gate_must_be_released_and_context_ready():
    assert eligible_gate_emotion(_gate(released=False), IDENTITY) == (None, False)
    assert eligible_gate_emotion(_gate(ready=False), IDENTITY) == (None, False)


def test_closed_window_and_previous_visual_state_are_not_reused():
    assert eligible_gate_emotion(
        _gate(window={"status": "closed", "owner": "service"}), IDENTITY
    ) == (None, False)
    assert eligible_gate_emotion(
        _gate(evaluation={"status": "missing", "last_valid": "happy"}), IDENTITY
    ) == (None, False)


def test_gate_requires_complete_identity_shape():
    assert eligible_gate_emotion(_gate(), {"session_id": "session-1"}) == (None, False)
