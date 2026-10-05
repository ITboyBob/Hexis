from __future__ import annotations

import asyncio
import json
import sys
import wave
from dataclasses import replace
from types import SimpleNamespace
from uuid import UUID, uuid4, uuid5
from urllib.parse import parse_qsl, urlsplit

import pytest

from apps.voice_realtime_harness import (
    ABehaviorEventSink,
    CGateFacts,
    CEmotionReleaseAdapter,
    HarnessRunResult,
    HarnessStageError,
    _UUID_NAMESPACE,
    _emotion_runtime_config,
    _default_connect_factory,
    _resolve_db_dsn,
    _run_real_harness,
    parse_args,
    read_wav,
    run_harness,
    sanitize_protocol_event,
    write_evidence,
)
from services.behavior_events import (
    BehaviorEvent,
    query_behavior_events,
    replay_behavior_events,
)
from services.qwen_realtime_protocol import ProtocolEvent
from services.qwen_realtime_protocol import ProtocolEvent, parse_event
from services.qwen_realtime_output import OutputPlaybackFacts
from services.voice_emotion_gate import VoiceEmotionCoordinator
from services.voice_first_response import run_first_response_turn
from services.voice_input_authorization import (
    CaptureRange,
    FinalTranscript,
    InputAuthorizationService,
    InputIdentity,
)


def write_wav(path, *, frames: int = 160) -> bytes:
    pcm = b"\x01\x00" * frames
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(pcm)
    return pcm


def config(tmp_path, *, wav_name: str = "input.wav"):
    return {
        "endpoint": "wss://qwen.example/realtime?foo=bar",
        "model": "qwen-realtime",
        "voice": "Tina",
        "api_key_env": "QWEN_REALTIME_API_KEY",
        "wav": str(tmp_path / wav_name),
        "session_id": str(uuid4()),
        "input_turn_id": str(uuid4()),
        "timeout": 2,
        "stable_instructions": "稳定层",
        "output_epoch": 1,
        "playback_id": "play-1",
    }


def runtime_config(tmp_path, *, wav_name: str = "input.wav"):
    value = config(tmp_path, wav_name=wav_name)
    value.update(
        {
            "db_dsn_env": "HEXIS_TEST_DSN",
            "memory_limit": 10,
            "emotion": {
                "provider": "openai-chat-completions-endpoint",
                "model": "qwen-plus",
                "endpoint": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                "api_key_env": "DASHSCOPE_API_KEY",
                "timeout_ms": 1000,
            },
        }
    )
    return value


class FakeWebSocket:
    def __init__(self, incoming: list[dict]):
        self.incoming: asyncio.Queue[str] = asyncio.Queue()
        for value in incoming:
            self.incoming.put_nowait(json.dumps(value))
        self.sent: list[dict] = []
        self.closed = False
        self.session_config = None

    async def send(self, message: str) -> None:
        value = json.loads(message)
        self.sent.append(value)
        if value.get("type") == "session.update":
            if value.get("event_id"):
                self.session_config.update(value["session"])
                self.incoming.put_nowait(
                    json.dumps(
                        {
                            "type": "session.updated",
                            "event_id": value["event_id"],
                            "session": {
                                **self.session_config,
                                "id": "cloud-session-1",
                            },
                        }
                    )
                )
            else:
                self.session_config = dict(value["session"])

    async def recv(self) -> str:
        return await self.incoming.get()

    async def close(self) -> None:
        self.closed = True


class FakeBehaviorDB:
    def __init__(self):
        self.rows: dict[str, tuple] = {}
        self.seq = 0
        self.queries = 0

    def acquire(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc_info):
        return False

    async def fetchval(self, _query, *args):
        self.queries += 1
        event_id = args[0]
        existing = self.rows.get(event_id)
        if existing is not None and existing != args:
            raise RuntimeError(f"behavior event id conflict: {event_id}")
        if existing is None:
            self.seq += 1
            self.rows[event_id] = args
            return json.dumps(
                {"event_id": str(event_id), "seq": self.seq, "deduplicated": False}
            )
        return json.dumps(
            {"event_id": str(event_id), "seq": self.seq, "deduplicated": True}
        )


def local_event(session_id=None):
    return BehaviorEvent(
        event_id=uuid4(),
        schema_version=1,
        event_type="voice_input_committed",
        stream_id="voice-first-response",
        session_id=session_id or uuid4(),
        source="server",
        producer_id="hexis.voice-realtime-harness",
        producer_seq=1,
        channel="voice",
        payload={"capture_instance_id": str(uuid4())},
        input_turn_id=str(uuid4()),
    )


def turn_fakes():
    identity = InputIdentity(
        session_id=uuid4(),
        input_turn_id=uuid4(),
        input_order=0,
        stimulus_revision=0,
    )
    source = CaptureRange(
        session_id=identity.session_id,
        media_session_id="media-1",
        capture_instance_id=uuid4(),
        start_frame=0,
        end_frame=160,
        start_monotonic_ms=0,
        end_monotonic_ms=10,
        audio_sha256="a" * 64,
    )
    final = FinalTranscript(
        identity=identity,
        capture_instance_id=source.capture_instance_id,
        media_session_id=source.media_session_id,
        item_id="item-1",
        transcript="请继续",
        transcript_version=1,
        is_final=True,
    )
    event = ProtocolEvent(
        type="input_transcript_final",
        item_id="item-1",
        transcript="请继续",
        is_final=True,
    )

    class Client:
        def __init__(self):
            self.commit_calls = 0
            self.create_calls = []
            self.pending_instructions = None

        async def commit_audio(self):
            self.commit_calls += 1

        async def wait_for_input_committed(self):
            return ProtocolEvent(
                type="input_audio_buffer_committed",
                item_id="item-1",
            )

        async def wait_for_final(self, item_id=None):
            if item_id is not None and item_id != event.item_id:
                raise AssertionError("final 未绑定到 commit 回执 item_id")
            return event

        async def update_session_instructions(self, instructions):
            self.pending_instructions = instructions

        async def create_first_response(self, response_id):
            self.create_calls.append((response_id, self.pending_instructions))

    return identity, source, final, Client()


def test_parser_accepts_only_explicit_paths():
    args = parse_args(
        [
            "--config",
            "/tmp/config.json",
            "--wav",
            "/tmp/input.wav",
            "--output",
            "/tmp/evidence.json",
        ]
    )
    assert args.config == "/tmp/config.json"
    assert args.wav == "/tmp/input.wav"
    assert args.output == "/tmp/evidence.json"
    with pytest.raises(SystemExit):
        parse_args(["--config", "/tmp/config.json", "--endpoint", "wss://example"])


def test_read_wav_keeps_only_facts_and_transient_pcm(tmp_path):
    pcm = write_wav(tmp_path / "input.wav", frames=160)

    audio = read_wav(tmp_path / "input.wav")

    assert audio.pcm_bytes == pcm
    assert audio.facts.frame_count == 160
    assert audio.facts.duration_seconds == pytest.approx(0.01)
    assert audio.facts.sample_rate == 16000
    assert audio.facts.channels == 1
    assert audio.facts.sample_width == 2
    assert len(audio.facts.audio_sha256) == 64


def test_protocol_evidence_redacts_sensitive_and_large_payloads():
    raw = {
        "type": "response.audio.delta",
        "response_id": "resp-1",
        "delta": "AAAA",
    }
    value = sanitize_protocol_event(
        parse_event(raw), source="simulated", direction="outgoing"
    )
    text = json.dumps(value, ensure_ascii=False)

    assert value["type"] == "response.audio.delta"
    assert value["source"] == "simulated"
    assert "raw" not in value
    assert "AAAA" not in text
    assert value["payload"]["audio"]["base64_length"] == 4
    assert len(value["payload"]["audio"]["sha256"]) == 64


def test_instruction_update_evidence_correlates_hashes_without_raw_prompt():
    request = {
        "type": "session.update",
        "event_id": "event-1",
        "session": {"instructions": "stable secret\n\nper-turn context"},
    }
    receipt = {
        "type": "session.updated",
        "event_id": "event-1",
        "session": {
            "id": "cloud-session-1",
            "instructions": "stable secret\n\nper-turn context",
        },
    }
    sent = sanitize_protocol_event(
        ProtocolEvent(type="session.update", raw=request),
        source="real",
        direction="outgoing",
    )
    received = sanitize_protocol_event(
        parse_event(receipt), source="real", direction="incoming"
    )
    encoded = json.dumps([sent, received], ensure_ascii=False)

    assert sent["payload"]["event_id"] == received["payload"]["event_id"]
    assert sent["payload"]["instructions"] == received["payload"]["instructions"]
    assert "stable secret" not in encoded


def test_protocol_evidence_records_generated_transcript_without_raw_text():
    raw = {
        "type": "response.audio_transcript.done",
        "response_id": "resp-1",
        "transcript": "你好，我在。",
    }
    value = sanitize_protocol_event(
        parse_event(raw), source="real", direction="incoming"
    )

    assert value["type"] == "response_audio_transcript_done"
    assert value["payload"]["transcript"]["preview"] == "你好，我在。"
    assert "raw" not in value


@pytest.mark.asyncio(loop_scope="function")
async def test_harness_success_creates_bounded_evidence(tmp_path, monkeypatch):
    wav = tmp_path / "input.wav"
    write_wav(wav)
    value = config(tmp_path)
    monkeypatch.setenv(value["api_key_env"], "secret-key-value")
    response_id = f"resp-{uuid5(_UUID_NAMESPACE, value['input_turn_id'])}"
    websocket = FakeWebSocket(
        [
            {"type": "session.updated", "session": {"id": "cloud-session-1"}},
            {"type": "vendor.unknown", "secret": "hidden"},
            {
                "type": "input_audio_buffer.committed",
                "item_id": "item-1",
            },
            {
                "type": "conversation.item.input_audio_transcription.completed",
                "item_id": "item-1",
                "transcript": "请继续",
                "is_final": True,
            },
            {"type": "response.created", "response": {"id": response_id}},
            {
                "type": "response.audio.delta",
                "response_id": response_id,
                "item_id": "item-audio-1",
                "output_index": 0,
                "delta": "AQIDBA==",
            },
            {"type": "response.done", "response": {"id": response_id}},
        ]
    )
    observed = {}

    async def connect_factory(endpoint, *, extra_headers):
        observed["endpoint"] = endpoint
        observed["authorization"] = extra_headers["Authorization"]
        return websocket

    async def context_reader(_identity):
        return {"identity": {}}

    async def evaluator(_message, _context):
        return {"label": "calm"}

    async def hydrator(_identity, _message):
        return SimpleNamespace(
            per_response_instructions="每轮上下文",
            instructions_hash="b" * 64,
            hydrate_ms=1.0,
            memory_count=1,
        )

    class FakePlaybackBridge:
        async def start(self, chunk):
            return OutputPlaybackFacts(
                started=True,
                start_ms=0,
                audible_ms=0,
            )

        async def finish(self):
            return OutputPlaybackFacts(
                terminal="ended",
                scope="complete",
                played_ms=0,
            )

    result = await run_harness(
        value,
        wav,
        connect_factory=connect_factory,
        coordinator=VoiceEmotionCoordinator(
            context_reader=context_reader,
            evaluator=evaluator,
            hydrator=hydrator,
            emotion_timeout_ms=1000,
        ),
        behavior_pool=FakeBehaviorDB(),
        source="simulated",
        playback_bridge=FakePlaybackBridge(),
    )

    assert result.ok is True
    assert observed["authorization"] == "Bearer secret-key-value"
    parsed_endpoint = urlsplit(observed["endpoint"])
    assert parse_qsl(parsed_endpoint.query) == [
        ("foo", "bar"),
        ("model", "qwen-realtime"),
    ]
    evidence = result.evidence
    serialized = json.dumps(evidence, ensure_ascii=False)
    assert evidence["schema"] == "voice-bob101-harness-v1"
    assert evidence["source"] == "simulated"
    assert evidence["cloud"]["model"] == "qwen-realtime"
    assert evidence["cloud"]["session_id"] == "cloud-session-1"
    assert evidence["audio"]["frame_count"] == 160
    assert evidence["final"]["transcript"] == "请继续"
    assert evidence["authorization_receipt"]["deduplicated"] is False
    assert evidence["c_release"]["released"] is True
    assert evidence["c_release"]["context_ready"] is True
    assert evidence["hydration"]["memory_count"] == 1
    assert evidence["response_created"] is True
    assert evidence["response_create_monotonic_ms"] >= 0
    assert evidence["response_stream"]["awaited_terminal"] is True
    assert evidence["response_stream"]["created"]["type"] == "response.created"
    assert evidence["response_stream"]["first_delta"]["type"] == "response.audio.delta"
    assert evidence["response_stream"]["terminal"]["type"] == "response.done"
    assert [item["event_type"] for item in evidence["behavior_events"]] == [
        "voice_commit_sent",
        "voice_transcript_final",
        "voice_input_committed",
        "voice_hydrate_done",
        "voice_response_created",
        "voice_first_audio_delta",
        "voice_playback_started",
        "voice_first_audible",
        "voice_response_done",
        "voice_playback_ended",
    ]
    assert evidence["unknown_events"][0]["type"] == "vendor.unknown"
    assert all(event["source"] == "simulated" for event in evidence["protocol_events"])
    assert "secret-key-value" not in serialized
    assert "AAAA" not in serialized
    assert "每轮上下文" not in serialized
    assert '"raw"' not in serialized
    assert websocket.sent[-1]["type"] == "response.create"
    assert websocket.closed is True


@pytest.mark.asyncio(loop_scope="session")
async def test_harness_full_chain_replays_from_a(db_pool, tmp_path, monkeypatch):
    wav = tmp_path / "input.wav"
    write_wav(wav)
    value = config(tmp_path)
    monkeypatch.setenv(value["api_key_env"], "secret-key-value")
    response_id = f"resp-{uuid5(_UUID_NAMESPACE, value['input_turn_id'])}"
    websocket = FakeWebSocket(
        [
            {"type": "session.updated", "session": {"id": "cloud-session-1"}},
            {"type": "input_audio_buffer.committed", "item_id": "item-1"},
            {
                "type": "conversation.item.input_audio_transcription.completed",
                "item_id": "item-1",
                "transcript": "请继续",
                "is_final": True,
            },
            {"type": "response.created", "response": {"id": response_id}},
            {
                "type": "response.audio.delta",
                "response_id": response_id,
                "item_id": "item-audio-1",
                "output_index": 0,
                "delta": "AQIDBA==",
            },
            {"type": "response.done", "response": {"id": response_id}},
        ]
    )

    async def connect_factory(_endpoint, *, extra_headers):
        return websocket

    async def context_reader(_identity):
        return {"identity": {}}

    async def evaluator(_message, _context):
        return {"label": "calm"}

    async def hydrator(_identity, _message):
        return SimpleNamespace(
            per_response_instructions="每轮上下文",
            instructions_hash="b" * 64,
            hydrate_ms=1.0,
            memory_count=1,
        )

    class FakePlaybackBridge:
        async def start(self, chunk):
            return OutputPlaybackFacts(started=True, start_ms=0, audible_ms=0)

        async def finish(self):
            return OutputPlaybackFacts(
                terminal="ended", scope="complete", played_ms=0
            )

    result = await run_harness(
        value,
        wav,
        connect_factory=connect_factory,
        coordinator=VoiceEmotionCoordinator(
            context_reader=context_reader,
            evaluator=evaluator,
            hydrator=hydrator,
            emotion_timeout_ms=1000,
        ),
        behavior_pool=db_pool,
        source="simulated",
        playback_bridge=FakePlaybackBridge(),
    )

    assert result.ok is True
    page = await query_behavior_events(
        db_pool, session_id=UUID(value["session_id"]), limit=100
    )
    replay = await replay_behavior_events(
        db_pool, session_id=UUID(value["session_id"]), limit=100
    )
    expected = [
        "voice_commit_sent",
        "voice_transcript_final",
        "voice_input_committed",
        "voice_hydrate_done",
        "voice_response_created",
        "voice_first_audio_delta",
        "voice_playback_started",
        "voice_first_audible",
        "voice_response_done",
        "voice_playback_ended",
    ]

    assert page["has_more"] is False
    assert page["truncated"] is False
    assert page["filter_omitted_count"] == 0
    assert [event["event_type"] for event in page["events"]] == expected
    assert all(
        event["input_turn_id"] == value["input_turn_id"]
        for event in page["events"]
    )
    assert replay["event_count"] == 10
    assert replay["filter_omitted_count"] == 0
    assert replay["has_more"] is False
    assert replay["streams"][0]["sequence_gaps"] == []
    assert [
        event["event_type"] for event in replay["streams"][0]["events"]
    ] == expected


@pytest.mark.asyncio(loop_scope="function")
async def test_harness_response_failed_is_explicit_not_timeout(tmp_path, monkeypatch):
    wav = tmp_path / "input.wav"
    write_wav(wav)
    value = config(tmp_path)
    monkeypatch.setenv(value["api_key_env"], "secret-key-value")
    websocket = FakeWebSocket(
        [
            {"type": "session.updated", "session": {"id": "cloud-session-1"}},
            {"type": "input_audio_buffer.committed", "item_id": "item-1"},
            {
                "type": "conversation.item.input_audio_transcription.completed",
                "item_id": "item-1",
                "transcript": "请继续",
                "is_final": True,
            },
            {
                "type": "response.failed",
                "response": {
                    "id": "resp-created",
                    "error": {"code": "server_error", "message": "Cloud failed"},
                },
            },
        ]
    )

    async def connect_factory(_endpoint, *, extra_headers):
        return websocket

    async def context_reader(_identity):
        return {"identity": {}}

    async def evaluator(_message, _context):
        return {"label": "calm"}

    async def hydrator(_identity, _message):
        return SimpleNamespace(per_response_instructions="每轮上下文")

    result = await run_harness(
        value,
        wav,
        connect_factory=connect_factory,
        coordinator=VoiceEmotionCoordinator(
            context_reader=context_reader,
            evaluator=evaluator,
            hydrator=hydrator,
            emotion_timeout_ms=1000,
        ),
        behavior_pool=FakeBehaviorDB(),
        source="simulated",
    )

    assert result.ok is False
    assert result.evidence["failure"]["stage"] == "response_stream"
    terminal = result.evidence["response_stream"]["terminal"]
    assert terminal["type"] == "response.failed"
    assert terminal["response_id"] == "resp-created"
    assert terminal["error_code"] == "server_error"
    assert terminal["error_message"] == "Cloud failed"
    assert isinstance(terminal["elapsed_ms"], int)


@pytest.mark.asyncio(loop_scope="function")
async def test_harness_requires_config_wav_to_match_explicit_wav(tmp_path, monkeypatch):
    wav = tmp_path / "input.wav"
    write_wav(wav)
    value = config(tmp_path)
    monkeypatch.setenv(value["api_key_env"], "secret-key-value")

    result = await run_harness(
        value,
        tmp_path / "other.wav",
        connect_factory=None,
        coordinator=object(),
        behavior_pool=object(),
        source="simulated",
    )

    assert result.ok is False
    assert result.evidence["failure"]["stage"] == "config"
    assert "wav" in result.evidence["failure"]["message"]


def test_resolve_db_dsn_requires_explicit_env_value(monkeypatch):
    monkeypatch.delenv("HEXIS_TEST_DSN", raising=False)

    with pytest.raises(HarnessStageError, match="HEXIS_TEST_DSN"):
        _resolve_db_dsn({"db_dsn_env": "HEXIS_TEST_DSN"})


def test_resolve_db_dsn_rejects_missing_explicit_source():
    with pytest.raises(HarnessStageError, match="db_source"):
        _resolve_db_dsn({})


def test_resolve_db_dsn_accepts_explicit_hexis_env(monkeypatch):
    dsn = "postgresql://user:password@localhost:43815/hexis_memory"
    monkeypatch.setattr(
        "apps.voice_realtime_harness.db_dsn_from_env", lambda: dsn
    )

    assert _resolve_db_dsn({"db_source": "hexis-env"}) == (dsn, "hexis-env")


def test_resolve_db_dsn_uses_explicit_env_value(monkeypatch):
    dsn = "postgresql://user:password@localhost:43815/hexis_memory"
    monkeypatch.setenv("HEXIS_TEST_DSN", dsn)

    assert _resolve_db_dsn({"db_dsn_env": "HEXIS_TEST_DSN"}) == (
        dsn,
        "HEXIS_TEST_DSN",
    )


def test_emotion_runtime_config_resolves_explicit_api_key(monkeypatch):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key")
    value = {
        "emotion": {
            "provider": "openai-chat-completions-endpoint",
            "model": "qwen-plus",
            "endpoint": "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "api_key_env": "DASHSCOPE_API_KEY",
            "timeout_ms": 1000,
        }
    }

    result = _emotion_runtime_config(value)

    assert result["model"] == "qwen-plus"
    assert result["resolved_api_key"] == "test-key"
    assert result["timeout_seconds"] == 1.0
    assert result["host_managed_timeout"] is True


@pytest.mark.asyncio(loop_scope="function")
async def test_default_connect_factory_uses_additional_headers(monkeypatch):
    observed = {}
    websocket = object()

    async def fake_connect(endpoint, **kwargs):
        observed["endpoint"] = endpoint
        observed["kwargs"] = kwargs
        return websocket

    monkeypatch.setitem(
        sys.modules, "websockets", SimpleNamespace(connect=fake_connect)
    )

    result = await _default_connect_factory(
        "wss://qwen.example/realtime",
        extra_headers={"Authorization": "Bearer test-key"},
    )

    assert result is websocket
    assert observed["endpoint"] == "wss://qwen.example/realtime"
    assert observed["kwargs"] == {
        "additional_headers": {"Authorization": "Bearer test-key"}
    }


@pytest.mark.asyncio(loop_scope="function")
async def test_run_real_harness_builds_and_closes_runtime_pool(tmp_path, monkeypatch):
    wav = tmp_path / "input.wav"
    write_wav(wav)
    value = runtime_config(tmp_path)
    monkeypatch.setenv("HEXIS_TEST_DSN", "postgresql://user:password@localhost/db")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key")

    class FakePool:
        def __init__(self):
            self.closed = False

        async def close(self):
            self.closed = True

    pool = FakePool()

    async def fake_create_pool(_dsn, **_kwargs):
        return pool

    def fake_build_runtime(_config, *, pool, session_id, stable_instructions):
        return object(), {
            "db_source": "HEXIS_TEST_DSN",
            "memory_limit": 10,
            "stable_instructions_hash": "a" * 64,
        }

    async def fake_run_harness(
        _config,
        _wav,
        *,
        coordinator,
        behavior_pool,
        source,
        playback_bridge=None,
    ):
        return HarnessRunResult(
            ok=True,
            evidence={
                "schema": "voice-bob101-harness-v1",
                "runtime_passed_pool": behavior_pool is pool,
                "runtime_source": source,
            },
        )

    import asyncpg

    monkeypatch.setattr(asyncpg, "create_pool", fake_create_pool)
    monkeypatch.setattr(
        "apps.voice_realtime_harness._build_runtime", fake_build_runtime
    )
    monkeypatch.setattr("apps.voice_realtime_harness.run_harness", fake_run_harness)

    result = await _run_real_harness(value, wav)

    assert result.ok is True
    assert result.evidence["runtime_passed_pool"] is True
    assert result.evidence["runtime_source"] == "real"
    assert result.evidence["runtime"]["db_source"] == "HEXIS_TEST_DSN"
    assert pool.closed is True


@pytest.mark.parametrize(
    "field",
    [
        "endpoint",
        "model",
        "voice",
        "api_key_env",
        "wav",
        "session_id",
        "input_turn_id",
        "timeout",
        "stable_instructions",
    ],
)
@pytest.mark.asyncio(loop_scope="function")
async def test_harness_rejects_missing_required_config(tmp_path, field):
    value = config(tmp_path)
    del value[field]

    result = await run_harness(
        value,
        value["wav"] if field != "wav" else tmp_path / "input.wav",
        connect_factory=None,
        coordinator=object(),
        behavior_pool=object(),
        source="simulated",
    )

    assert result.ok is False
    assert result.evidence["failure"]["stage"] == "config"
    assert field in result.evidence["failure"]["message"]


@pytest.mark.asyncio(loop_scope="function")
async def test_harness_missing_explicit_api_key_fails_without_leaking_value(
    tmp_path, monkeypatch
):
    wav = tmp_path / "input.wav"
    write_wav(wav)
    value = config(tmp_path)
    monkeypatch.delenv(value["api_key_env"], raising=False)

    result = await run_harness(
        value,
        wav,
        connect_factory=None,
        coordinator=object(),
        behavior_pool=object(),
        source="simulated",
    )

    assert result.ok is False
    assert result.evidence["failure"]["stage"] == "api_key"
    assert value["api_key_env"] in result.evidence["failure"]["message"]


@pytest.mark.asyncio(loop_scope="function")
async def test_harness_connection_failure_is_structured_as_connection(
    tmp_path, monkeypatch
):
    wav = tmp_path / "input.wav"
    write_wav(wav)
    value = config(tmp_path)
    monkeypatch.setenv(value["api_key_env"], "secret-key-value")

    async def connect_factory(_endpoint, *, extra_headers):
        raise OSError("cloud unavailable")

    result = await run_harness(
        value,
        wav,
        connect_factory=connect_factory,
        coordinator=object(),
        behavior_pool=object(),
        source="simulated",
    )

    assert result.ok is False
    assert result.evidence["response_created"] is False
    assert result.evidence["failure"] == {
        "stage": "connection",
        "error_type": "QwenRealtimeConnectionError",
        "message": "Qwen Realtime WebSocket 连接失败",
    }
    assert "secret-key-value" not in json.dumps(result.evidence, ensure_ascii=False)


def test_write_evidence_does_not_overwrite_existing_output(tmp_path):
    output = tmp_path / "evidence.json"
    output.write_text("existing", encoding="utf-8")
    evidence = {"schema": "voice-bob101-harness-v1"}

    with pytest.raises(RuntimeError, match="已存在"):
        write_evidence(output, evidence)
    assert output.read_text(encoding="utf-8") == "existing"


def test_main_returns_failure_code_and_writes_failure_evidence(tmp_path):
    wav = tmp_path / "input.wav"
    write_wav(wav)
    value = config(tmp_path)
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(value), encoding="utf-8")
    output = tmp_path / "evidence.json"

    async def runner(*_args, **_kwargs):
        return HarnessRunResult(
            ok=False,
            evidence={
                "schema": "voice-bob101-harness-v1",
                "failure": {
                    "stage": "turn",
                    "error_type": "RuntimeError",
                    "message": "失败",
                },
            },
        )

    from apps.voice_realtime_harness import main

    code = main(
        [
            "--config",
            str(config_path),
            "--wav",
            str(wav),
            "--output",
            str(output),
        ],
        runner=runner,
    )

    assert code == 1
    stored = json.loads(output.read_text(encoding="utf-8"))
    assert stored["failure"]["stage"] == "turn"


@pytest.mark.asyncio(loop_scope="function")
async def test_a_adapter_uses_stable_event_id_and_preserves_a_receipts():
    db = FakeBehaviorDB()
    sink = ABehaviorEventSink(db)
    event = local_event()

    first = await sink(event)
    retry = await sink(event)

    assert first["event_id"] == retry["event_id"]
    assert first["seq"] == 1
    assert first["deduplicated"] is False
    assert retry["seq"] == 1
    assert retry["deduplicated"] is True
    assert sink.receipts == [first, retry]
    assert len(db.rows) == 1


@pytest.mark.asyncio(loop_scope="function")
async def test_a_adapter_propagates_event_conflict_fail_loud():
    db = FakeBehaviorDB()
    sink = ABehaviorEventSink(db)
    event = local_event()
    await sink(event)

    changed = replace(event, payload={"changed": True})
    with pytest.raises(RuntimeError, match="behavior event id conflict"):
        await sink(changed)


@pytest.mark.asyncio(loop_scope="function")
async def test_c_release_later_than_hydration_waits_before_response():
    release_gate = asyncio.Event()
    hydration_done = asyncio.Event()

    async def context_reader(_identity):
        return {"identity": {}}

    async def evaluator(_message, _context):
        await release_gate.wait()
        return {"label": "calm"}

    async def hydrator(_identity, _message):
        hydration_done.set()
        return SimpleNamespace(per_response_instructions="每轮上下文")

    coordinator = VoiceEmotionCoordinator(
        context_reader=context_reader,
        evaluator=evaluator,
        hydrator=hydrator,
        emotion_timeout_ms=1000,
    )
    identity, source, final, client = turn_fakes()
    adapter = CEmotionReleaseAdapter(coordinator)
    adapter.start(
        {
            "session_id": str(identity.session_id),
            "input_turn_id": str(identity.input_turn_id),
            "input_order": 0,
            "stimulus_revision": 0,
        },
        final.transcript,
    )

    async def event_sink(_event):
        return None

    turn = asyncio.create_task(
        run_first_response_turn(
            authorization_service=InputAuthorizationService(),
            final_transcript=final,
            capture_range=source,
            release=adapter.release(),
            hydration=adapter.hydration(),
            client=client,
            event_sink=event_sink,
            response_id="resp-1",
        )
    )
    await asyncio.wait_for(hydration_done.wait(), timeout=0.2)
    await asyncio.sleep(0)
    assert client.create_calls == []

    release_gate.set()
    result = await asyncio.wait_for(turn, timeout=0.2)

    assert result.ok is True
    assert client.create_calls == [("resp-1", "每轮上下文")]


@pytest.mark.asyncio(loop_scope="function")
async def test_c_ordinary_error_releases_without_visual_window_facts():
    async def context_reader(_identity):
        return {"identity": {}}

    async def evaluator(_message, _context):
        raise RuntimeError("provider unavailable")

    async def hydrator(_identity, _message):
        return SimpleNamespace(per_response_instructions="每轮上下文")

    coordinator = VoiceEmotionCoordinator(
        context_reader=context_reader,
        evaluator=evaluator,
        hydrator=hydrator,
        emotion_timeout_ms=1000,
    )
    adapter = CEmotionReleaseAdapter(coordinator)
    adapter.start(
        {
            "session_id": str(uuid4()),
            "input_turn_id": str(uuid4()),
            "input_order": 0,
            "stimulus_revision": 0,
        },
        "请继续",
    )

    facts = await adapter.release()
    hydration = await adapter.hydration()

    assert facts == CGateFacts(released=True, context_ready=True)
    assert not hasattr(facts, "visual_window")
    assert hydration.per_response_instructions == "每轮上下文"
    assert adapter.last_result.visual_window["status"] == "open"
