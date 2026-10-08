from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from services.voice_realtime_transport import (
    BargeInCapability,
    ClientTransportError,
    RealtimeSessionResponse,
    VoiceClientTransport,
    build_barge_in_capability,
    load_playback_coordination_config,
    parse_client_message,
    parse_server_message,
)
from services.voice_realtime_session import VoiceRealtimeSession, VoiceRealtimeSessionError


@pytest.fixture(scope="module", autouse=True)
def temp_test_db():
    yield


@pytest.fixture(scope="module", autouse=True)
def sync_test_embedding_dimension_from_db():
    yield


@pytest.fixture(scope="module", autouse=True)
def configure_agent_for_tests():
    yield


@pytest.fixture(scope="module", autouse=True)
def apply_repo_migrations():
    yield


@pytest.fixture(autouse=True)
def setup_db():
    yield


def playback_receipt(
    *,
    action: str,
    session_id: str,
    media_session_id: str,
    response_id: str = "response-1",
    playback_id: str = "playback-1",
    producer_seq: int,
    output_epoch: int = 1,
    **action_fields,
):
    return {
        "action": action,
        "session_id": session_id,
        "response_id": response_id,
        "playback_id": playback_id,
        "producer_seq": producer_seq,
        "output_epoch": output_epoch,
        "media_session_id": media_session_id,
        **action_fields,
    }


def confirmed_message(
    *,
    session_id: str,
    media_session_id: str,
    capture_instance_id: str,
    producer_seq: int,
    local_playback_state: str = "not_started",
    receipt_prefix: list[dict[str, object]] | None = None,
):
    return {
        "schema_version": 1,
        "type": "barge_in_confirmed",
        "session_id": session_id,
        "media_session_id": media_session_id,
        "producer_seq": producer_seq,
        "command_id": str(uuid4()),
        "capture_instance_id": capture_instance_id,
        "response_id": "response-1",
        "output_epoch": 1,
        "playback_id": "playback-1",
        "local_playback_state": local_playback_state,
        "receipt_prefix": receipt_prefix or [],
    }


def test_loads_the_existing_c7_snapshot_and_builds_one_shared_capability():
    config = load_playback_coordination_config()
    capability = build_barge_in_capability(config, supported=True)

    assert config.configuration_id == "voice-barge-in-playback-initial-v1"
    assert config.maximum_receipt_prefix_items == 256
    assert capability.model_dump(mode="json") == {
        "supported": True,
        "configuration_id": config.configuration_id,
        "maximum_receipt_prefix_items": config.maximum_receipt_prefix_items,
    }
    assert isinstance(capability, BargeInCapability)


def test_rest_session_description_and_websocket_ready_share_the_exact_capability():
    session_id = uuid4()
    config = load_playback_coordination_config()
    capability = build_barge_in_capability(config, supported=True)
    response = RealtimeSessionResponse(
        session_id=session_id,
        media_session_id="media-1",
        websocket_url="ws://127.0.0.1:43817/api/voice/realtime/media-1",
        barge_in_capability=capability,
    )
    ready = parse_server_message(
        {
            "schema_version": 1,
            "type": "ready",
            "session_id": str(session_id),
            "media_session_id": response.media_session_id,
            "producer_seq": 0,
            "cloud_session_id": "qwen-session",
            "instructions_hash": "a" * 64,
            "barge_in_capability": capability.model_dump(mode="json"),
        }
    )

    assert response.barge_in_capability == ready.barge_in_capability
    assert response.model_dump(mode="json")["session_id"] == str(session_id)


def test_ready_requires_capability_and_rejects_extra_capability_fields():
    ready = {
        "schema_version": 1,
        "type": "ready",
        "session_id": str(uuid4()),
        "media_session_id": "media-1",
        "producer_seq": 0,
        "cloud_session_id": "qwen-session",
        "instructions_hash": "a" * 64,
    }
    with pytest.raises(ValidationError):
        parse_server_message(ready)
    with pytest.raises(ValidationError):
        parse_server_message({
            **ready,
            "barge_in_capability": {
                "supported": True,
                "configuration_id": "voice-barge-in-playback-initial-v1",
                "maximum_receipt_prefix_items": 256,
                "unknown": "field",
            },
        })


def test_barge_in_command_cache_ignores_outer_sequence_but_rejects_changed_body():
    session_id = uuid4()
    capture_id = uuid4()
    session = VoiceRealtimeSession(
        session_id=session_id,
        media_session_id="media-1",
        client=object(),
        playback_coordination_config=load_playback_coordination_config(),
    )
    raw = confirmed_message(
        session_id=str(session_id),
        media_session_id="media-1",
        capture_instance_id=str(capture_id),
        producer_seq=4,
    )
    message = parse_client_message(raw)
    result = {
        "schema_version": 1,
        "type": "barge_in_result",
        "session_id": str(session_id),
        "media_session_id": "media-1",
        "producer_seq": 1,
        "command_id": str(message.command_id),
        "capture_instance_id": str(capture_id),
        "response_id": "response-1",
        "output_epoch": 1,
        "playback_id": "playback-1",
        "status": "accepted",
    }
    session.cache_barge_in_result(message, raw, result)

    retry = {**raw, "producer_seq": 5}
    assert session.cached_barge_in_result(parse_client_message(retry), retry) == result

    changed = {**retry, "playback_id": "different-playback"}
    with pytest.raises(VoiceRealtimeSessionError, match="different.*content"):
        session.cached_barge_in_result(parse_client_message(changed), changed)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda raw: raw.update(maximum_receipt_prefix_items=True),
        lambda raw: raw.update(maximum_receipt_prefix_items=0),
        lambda raw: raw.update(maximum_receipt_prefix_items=1.5),
        lambda raw: raw.update(unexpected=True),
    ],
)
def test_rejects_invalid_c7_snapshots(tmp_path, mutation):
    c7_path = Path(__file__).resolve().parents[2] / "config/voice-barge-in/playback-coordination.json"
    raw = json.loads(c7_path.read_text(encoding="utf-8"))
    mutation(raw)
    path = tmp_path / "playback-coordination.json"
    path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises((ValueError, ValidationError)):
        load_playback_coordination_config(path)


def test_fails_c7_load_when_the_selected_file_is_missing(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_playback_coordination_config(tmp_path / "missing.json")


@pytest.mark.parametrize("status", ["accepted", "playback_only", "stale_target", "unknown"])
def test_parses_exact_barge_in_result_identity_and_status(status):
    result = parse_server_message(
        {
            "schema_version": 1,
            "type": "barge_in_result",
            "session_id": str(uuid4()),
            "media_session_id": "media-1",
            "producer_seq": 1,
            "command_id": str(uuid4()),
            "capture_instance_id": str(uuid4()),
            "response_id": "response-1",
            "output_epoch": 1,
            "playback_id": "playback-1",
            "status": status,
        }
    )
    assert result.type == "barge_in_result"
    assert result.status == status

    with pytest.raises(ValidationError):
        parse_server_message({**result.model_dump(mode="json"), "unsupported": True})


def test_confirm_accepts_not_started_only_with_empty_prefix_and_an_open_capture():
    session_id = uuid4()
    capture_id = uuid4()
    transport = VoiceClientTransport(
        session_id=session_id,
        media_session_id="media-1",
        max_receipt_prefix_items=2,
    )
    opened = parse_client_message(
        {
            "schema_version": 1,
            "type": "capture_open",
            "session_id": str(session_id),
            "media_session_id": "media-1",
            "producer_seq": 0,
            "capture_instance_id": str(capture_id),
            "sample_rate": 16000,
            "channels": 1,
            "sample_format": "s16le",
        }
    )
    transport.accept(opened)
    message = parse_client_message(
        confirmed_message(
            session_id=str(session_id),
            media_session_id="media-1",
            capture_instance_id=str(capture_id),
            producer_seq=1,
        )
    )

    result = transport.accept(message)
    assert result["accepted"] is True
    assert result["action"] == "barge_in_confirmed"


def test_confirm_started_carries_real_open_and_terminal_interrupt_prefix():
    session_id = uuid4()
    capture_id = uuid4()
    transport = VoiceClientTransport(
        session_id=session_id,
        media_session_id="media-1",
        max_receipt_prefix_items=2,
    )
    transport.accept(
        parse_client_message(
            {
                "schema_version": 1,
                "type": "capture_open",
                "session_id": str(session_id),
                "media_session_id": "media-1",
                "producer_seq": 0,
                "capture_instance_id": str(capture_id),
                "sample_rate": 16000,
                "channels": 1,
                "sample_format": "s16le",
            }
        )
    )
    prefix = [
        playback_receipt(
            action="open", session_id=str(session_id), media_session_id="media-1",
            producer_seq=1, start_ms=10,
        ),
        playback_receipt(
            action="interrupt", session_id=str(session_id), media_session_id="media-1",
            producer_seq=2, played_ms=40, reason="barge_in",
        ),
    ]
    # The validated values are semantically equivalent but not lexically identical;
    # the result must carry the accepted raw emitter request without normalization.
    prefix[0]["session_id"] = str(session_id).upper()
    prefix[0]["output_epoch"] = True
    prefix[0]["start_ms"] = "10"
    raw_message = confirmed_message(
        session_id=str(session_id),
        media_session_id="media-1",
        capture_instance_id=str(capture_id),
        producer_seq=1,
        local_playback_state="started",
        receipt_prefix=prefix,
    )
    message = parse_client_message(raw_message)

    accepted = transport.accept(message, raw_message=raw_message)
    assert accepted["accepted"] is True
    assert accepted["receipt_prefix"] == prefix


def test_confirm_rejects_prefix_identity_gaps_pause_and_c7_overflow():
    session_id = uuid4()
    capture_id = uuid4()
    transport = VoiceClientTransport(
        session_id=session_id,
        media_session_id="media-1",
        max_receipt_prefix_items=1,
    )
    transport.accept(
        parse_client_message(
            {
                "schema_version": 1,
                "type": "capture_open",
                "session_id": str(session_id),
                "media_session_id": "media-1",
                "producer_seq": 0,
                "capture_instance_id": str(capture_id),
                "sample_rate": 16000,
                "channels": 1,
                "sample_format": "s16le",
            }
        )
    )
    capture_range = playback_receipt(
        action="interrupt", session_id=str(session_id), media_session_id="media-1",
        producer_seq=1, played_ms=40, reason="barge_in",
    )
    too_many = [capture_range, {**capture_range, "producer_seq": 2}]
    overflow = parse_client_message(
        confirmed_message(
            session_id=str(session_id),
            media_session_id="media-1",
            capture_instance_id=str(capture_id),
            producer_seq=1,
            local_playback_state="started",
            receipt_prefix=too_many,
        )
    )
    with pytest.raises(ClientTransportError, match="prefix"):
        transport.accept(overflow)

    wrong_target = [
        {**capture_range, "response_id": "other-response"},
    ]
    with pytest.raises(ValidationError):
        parse_client_message(
            confirmed_message(
                session_id=str(session_id),
                media_session_id="media-1",
                capture_instance_id=str(capture_id),
                producer_seq=1,
                local_playback_state="started",
                receipt_prefix=wrong_target,
            )
        )

    suspended_transport = VoiceClientTransport(
        session_id=session_id,
        media_session_id="media-1",
        max_receipt_prefix_items=1,
    )
    suspended_transport.accept(
        parse_client_message(
            {
                "schema_version": 1,
                "type": "capture_open",
                "session_id": str(session_id),
                "media_session_id": "media-1",
                "producer_seq": 0,
                "capture_instance_id": str(capture_id),
                "sample_rate": 16000,
                "channels": 1,
                "sample_format": "s16le",
            }
        )
    )
    suspend = parse_client_message(
        {
            "schema_version": 1,
            "type": "capture_suspend",
            "session_id": str(session_id),
            "media_session_id": "media-1",
            "producer_seq": 1,
            "capture_instance_id": str(capture_id),
            "control_id": str(uuid4()),
        }
    )
    suspended_transport.accept(suspend)
    confirmation = parse_client_message(
        confirmed_message(
            session_id=str(session_id),
            media_session_id="media-1",
            capture_instance_id=str(capture_id),
            producer_seq=2,
        )
    )
    assert suspended_transport.accept(confirmation)["accepted"] is True
    with pytest.raises(ClientTransportError, match="suspended"):
        suspended_transport.validate_barge_in_capture(confirmation.capture_instance_id)
