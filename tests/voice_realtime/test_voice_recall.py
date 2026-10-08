import asyncio
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
import httpx

from services.voice_recall import VOICE_RECALL_TOOL, VoiceRecallError, VoiceRecallService


class _Recall:
    def __init__(self, _pool):
        self.pool = _pool

    async def recall(self, query, **kwargs):
        self.query = query
        self.kwargs = kwargs
        return SimpleNamespace(memories=[], partial_activations=[])


@pytest.mark.asyncio
async def test_voice_recall_uses_read_only_recall_with_bounded_query(monkeypatch):
    import services.voice_recall as module

    monkeypatch.setattr(module, "CognitiveMemory", _Recall)
    pool = object()
    service = VoiceRecallService(pool)

    result = await service.execute(
        session_id=uuid4(),
        input_turn_id=uuid4(),
        name="voice_recall",
        arguments='{"query":"  project decision  "}',
    )

    assert json.loads(result) == {
        "status": "no_results",
        "query": "project decision",
        "memories": [],
        "partial_activations": [],
    }
    assert (
        VOICE_RECALL_TOOL["function"]["parameters"]["additionalProperties"]
        is False
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name,arguments",
    [
        ("other_tool", '{"query":"x"}'),
        ("voice_recall", "not-json"),
        ("voice_recall", '{"query":"x","limit":100}'),
        ("voice_recall", '{"query":""}'),
    ],
)
async def test_voice_recall_rejects_unapproved_or_malformed_calls(
    monkeypatch, name, arguments
):
    import services.voice_recall as module

    monkeypatch.setattr(module, "CognitiveMemory", _Recall)
    service = VoiceRecallService(object())
    call = service.execute(
        session_id=uuid4(),
        input_turn_id=uuid4(),
        name=name,
        arguments=arguments,
    )
    with pytest.raises(VoiceRecallError):
        await call


@pytest.mark.asyncio(loop_scope="session")
async def test_voice_recall_tool_activity_persists_request_and_result_for_the_origin_session(db_pool, monkeypatch):
    import apps.hexis_api as hexis_api
    from services.behavior_events import query_behavior_events
    from services.voice_realtime_session import VoiceRealtimeSession

    session_id = uuid4()
    media_session_id = f"media-{uuid4()}"
    input_turn_id = str(uuid4())
    response_id = "provider-response-1"
    call_id = f"voice-call-{uuid4()}"

    class Client:
        async def close(self):
            return None

    session = VoiceRealtimeSession(
        session_id=session_id,
        media_session_id=media_session_id,
        client=Client(),
    )

    monkeypatch.setattr(hexis_api, "_pool", db_pool)
    hexis_api._bind_voice_behavior_event_sink(session, db_pool)
    arguments = {"query": "Hexis"}
    output = {
        "status": "found",
        "query": "Hexis",
        "memories": [{"content": "V" * 2100}],
        "partial_activations": [],
    }

    await hexis_api._emit_voice_tool_activity(
        session,
        event_type="TOOL_START",
        call_id=call_id,
        response_id=response_id,
        input_turn_id=input_turn_id,
        stimulus_revision=4,
        tool_name="voice_recall",
        arguments=arguments,
    )
    await hexis_api._emit_voice_tool_activity(
        session,
        event_type="TOOL_RESULT",
        call_id=call_id,
        response_id=response_id,
        input_turn_id=input_turn_id,
        stimulus_revision=4,
        tool_name="voice_recall",
        arguments=arguments,
        success=True,
        output=output,
    )
    await hexis_api._emit_voice_tool_activity(
        session,
        event_type="TOOL_RESULT",
        call_id="remember-accepted-call",
        response_id=response_id,
        input_turn_id=input_turn_id,
        stimulus_revision=4,
        tool_name="remember",
        arguments={"content": "queue this memory", "type": "semantic"},
        success=True,
        output={"state": "accepted"},
        state="accepted",
    )

    persisted = await query_behavior_events(
        db_pool,
        session_id=session_id,
        event_type="TOOL_START",
        call_id=call_id,
    )
    assert len(persisted["events"]) == 1
    start_id = persisted["events"][0]["event_id"]

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=hexis_api.app),
        base_url="http://test",
    ) as client:
        response = await client.get("/api/chat/activity", params={"session_id": str(session_id)})
    assert response.status_code == 200
    rows = response.json()["events"]
    assert [row["event_type"] for row in rows] == ["TOOL_START", "TOOL_RESULT", "TOOL_RESULT"]
    assert [row["call_id"] for row in rows] == [call_id, call_id, "remember-accepted-call"]
    assert len({row["event_id"] for row in rows}) == 3
    assert rows[0]["event_id"] == start_id
    assert rows[0]["activity"]["detail"] == '{"query": "Hexis"}'
    assert rows[1]["activity"]["title"] == "voice_recall"
    assert len(rows[1]["activity"]["detail"]) == 2003
    assert rows[2]["activity"] is None
    assert response.json()["cursor"] == {"voice-realtime-session": 3}


@pytest.mark.asyncio(loop_scope="session")
@pytest.mark.parametrize("failed", [False, True])
@pytest.mark.parametrize("tool_name", ["voice_recall", "remember"])
async def test_talk_disconnect_preserves_dispatched_tool_receipt_and_replay(db_pool, monkeypatch, failed, tool_name):
    """在真实 WS 输出任务暂停于 handler 时断开，回执仍可从 DB 重放。"""
    from unittest.mock import AsyncMock
    from starlette.websockets import WebSocketDisconnect
    import apps.hexis_api as api
    import services.voice_recall as recall_module
    from services.qwen_realtime_protocol import ProtocolEvent
    from services.voice_conversation_history import VoiceConversationHistory
    from services.voice_realtime_session import VoiceRealtimeSession, VoiceRealtimeSessionRegistry
    from services.voice_realtime_transport import BargeInCapability

    entered, release, attached = asyncio.Event(), asyncio.Event(), asyncio.Event()
    session_id, input_id = uuid4(), uuid4()
    call_id, response_id = f"call-{uuid4()}", f"response-{uuid4()}"
    client = SimpleNamespace(close=AsyncMock())
    session = VoiceRealtimeSession(
        session_id=session_id, media_session_id=f"media-{uuid4()}", client=client,
        barge_in_capability=BargeInCapability(supported=True, configuration_id="test", maximum_receipt_prefix_items=256),
    )
    session._ready = True
    session.cloud_session_id = "controlled-cloud-session"
    session.session_execution_policy = None
    turn_id = await VoiceConversationHistory(db_pool).start_turn(
        session_id=session_id, input_turn_id=input_id, input_order=1,
        stimulus_revision=1, user_text="recall Hexis", context_version="test",
    )
    session._voice_turns[str(input_id)] = {"turn_id": turn_id, "input_order": 1, "stimulus_revision": 1}
    session.tool_surface = "m5"
    session._authorized_input_ids.add(str(input_id))
    session._response_input_ids[response_id] = str(input_id)
    session._input_gate_results[str(input_id)] = SimpleNamespace(
        released=True, context_ready=True,
        identity={"session_id": str(session_id), "input_turn_id": str(input_id), "input_order": 1, "stimulus_revision": 1},
    )
    session.start_voice_tool_followup = AsyncMock()
    registry = VoiceRealtimeSessionRegistry()
    registry.register(session_id, session)
    monkeypatch.setattr(api, "_pool", db_pool)
    monkeypatch.setattr(api, "_voice_realtime_registry", registry)
    monkeypatch.setenv("HEXIS_UI_ORIGIN", "http://localhost:3477")
    api._bind_voice_behavior_event_sink(session, db_pool)

    arguments = {"query": "Hexis"} if tool_name == "voice_recall" else {"content": "断开后保留回执", "type": "semantic"}

    async def delayed_recall(self, **kwargs):
        entered.set()
        await release.wait()
        if failed:
            raise RuntimeError("controlled recall failure")
        return json.dumps({"status": "no_results", "query": "Hexis", "memories": [], "partial_activations": []})
    monkeypatch.setattr(recall_module.VoiceRecallService, "execute", delayed_recall)
    executor = None
    if tool_name == "remember":
        from core.tools import create_default_registry
        from core.tools.base import ToolErrorType, ToolResult
        from services.lightweight_memory_executor import LightweightMemoryExecutor

        class DelayedExecutor(LightweightMemoryExecutor):
            async def submit_lightweight(self, call, arguments):
                entered.set()
                await release.wait()
                return await super().submit_lightweight(call, arguments)

        tool_registry = create_default_registry(db_pool)
        handler = tool_registry.get("remember")
        result = (
            ToolResult.error_result("受控失败原因", error_type=ToolErrorType.INVALID_PARAMS)
            if failed else ToolResult.success_result({"memory_id": str(uuid4()), "reused": False}, "记忆已保存")
        )
        monkeypatch.setattr(handler, "execute", AsyncMock(return_value=result))
        executor = DelayedExecutor(db_pool, registry=tool_registry)
        monkeypatch.setattr(api, "_m5_lightweight_executor", executor)

    class Socket:
        headers = {"origin": "http://localhost:3477"}
        disconnected = False
        async def accept(self):
            pass
        async def send_json(self, value):
            if self.disconnected:
                raise WebSocketDisconnect()
            if value["type"] == "ready":
                attached.set()
        async def receive_text(self):
            await entered.wait()
            self.disconnected = True
            raise WebSocketDisconnect()
        async def close(self, **kwargs):
            pass

    socket = Socket()
    media_task = asyncio.create_task(api.voice_realtime_media(socket, session.media_session_id))
    response_task = None
    try:
        await asyncio.wait_for(attached.wait(), 2)
        events = session.subscribe_response(response_id)
        for pending_call_id in (call_id, f"undispatched-{call_id}"):
            events.put_nowait(ProtocolEvent(
                type="response.function_call_arguments.done", response_id=response_id,
                call_id=pending_call_id, tool_name=tool_name, tool_arguments=json.dumps(arguments),
            ))
        events.put_nowait(ProtocolEvent(type="response.done", response_id=response_id, status="completed"))
        await session._response_dispatch({"response_id": response_id, "playback_id": "playback-test",
                                          "output_epoch": 1, "input_turn_id": str(input_id), "events": events})
        response_task = next(iter(session._background_tasks))
        receiver_task = asyncio.create_task(asyncio.Event().wait())
        session.register_background_task(receiver_task)
        await asyncio.wait_for(entered.wait(), 2)
        await asyncio.wait_for(media_task, 2)
        assert not response_task.cancelled()
        assert not response_task.done(), "Talk close cancelled the dispatched handler before its receipt"
        assert receiver_task.cancelled()
        release.set()
        await asyncio.wait_for(response_task, 2)
        assert not session.ready
        client.close.assert_awaited_once()
        session.start_voice_tool_followup.assert_not_awaited()
        if executor is not None:
            assert await executor.process_one() is True
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url="http://test") as browser:
            replay = await browser.get("/api/chat/activity", params={"session_id": str(session_id)})
        assert replay.status_code == 200
        rows = replay.json()["events"]
        visible_rows = [row for row in rows if row["activity"] is not None]
        assert [row["event_type"] for row in visible_rows] == ["TOOL_START", "TOOL_RESULT"]
        assert [row["call_id"] for row in visible_rows] == [call_id, call_id]
        assert visible_rows[1]["activity"]["kind"] == "tool_result"
        if tool_name == "voice_recall":
            assert visible_rows[1]["activity"]["detail"] == (
                "FAILED: Voice memory recall could not be completed." if failed else "未召回记忆"
            )
        else:
            assert len(rows) == 3
            assert visible_rows[1]["payload"]["state"] == ("failed" if failed else "done")
        assert not session._tool_receipt_tasks
    finally:
        release.set()
        for task in (media_task, response_task):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(*(task for task in (media_task, response_task) if task is not None), return_exceptions=True)
