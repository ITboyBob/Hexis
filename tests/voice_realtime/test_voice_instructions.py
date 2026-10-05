from __future__ import annotations

from services.voice_instructions import load_m4_instructions


def test_loader_extracts_only_the_two_protocol_carriers():
    bundle = load_m4_instructions()

    assert "# Voice Conversation Instructions" in bundle.stable
    assert "## Recall" in bundle.stable
    assert "TaskBridge Tools" not in bundle.stable
    assert "Memory Submissions" not in bundle.stable
    assert "来源：" not in bundle.stable
    assert "## Conversation Continuity Packet" in bundle.per_turn_template
    assert "{validated_emotion_label" in bundle.per_turn_template


def test_emotion_is_exact_and_optional():
    from services.voice_instructions import render_emotion

    assert render_emotion("happy", eligible=True) == "## Emotion\nhappy"
    for value in ("Happy", " happy", "happy ", "joyful", "happy!", None, 7):
        assert render_emotion(value, eligible=True) == ""
    assert render_emotion("happy", eligible=False) == ""
