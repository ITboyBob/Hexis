"""复用后台工具边界，音频输入不伪造原始用户文字。"""

import json
from time import perf_counter
from datetime import datetime, timezone
from uuid import UUID, uuid5

from services.behavior_events import BehaviorEvent, record_behavior_event_on_connection
from services.m5_tool_bridge import (
    execute_bound_tool,
    lightweight_memory_handlers,
    pend_message_handler,
)
from services.m5_task_service import M5TaskService
from services.voice_conversation_history import VoiceConversationHistory
from services.voice_recall import VoiceRecallService


def make_rtc_tool_executor(pool_provider, worker_provider, lightweight_provider):
    async def execute(session, call):
        session.validate_voice_tool_call(call)
        pool = pool_provider()
        response = session.responses[call.response_id]
        identity = session.inputs[response["item"]]
        history = VoiceConversationHistory(pool)
        arguments = json.loads(call.raw_arguments)
        if not isinstance(arguments, dict):
            raise ValueError("RTC tool arguments must be an object")
        saved = await history.register_tool_call(
            turn_id=identity["turn_id"],
            call_id=call.actual_call_id,
            tool_name=call.name,
            arguments=arguments,
        )
        if saved.get("status") == "completed":
            return saved["output"]
        if saved.get("status") != "registered":
            raise ValueError("Tool execution is uncertain; it will not be repeated")

        async def activity(kind, success=None, output=None):
            payload = {
                "tool_name": call.name,
                "call_id": call.actual_call_id,
                "arguments": arguments,
                "response_id": call.response_id,
            }
            if kind == "TOOL_RESULT":
                payload.update(
                    success=success, output=output, display_output=None, error=None
                )
            async with pool.acquire() as conn:
                await record_behavior_event_on_connection(
                    conn,
                    BehaviorEvent(
                        event_id=uuid5(call.command_id, kind),
                        schema_version=1,
                        event_type=kind,
                        stream_id=f"rtc-tool:{session.media_session_id}:{call.actual_call_id}",
                        session_id=session.session_id,
                        source="voice-rtc-host",
                        producer_id="hexis.voice-rtc",
                        producer_seq=0 if kind == "TOOL_START" else 1,
                        channel="voice",
                        payload=payload,
                        ts=datetime.now(timezone.utc),
                        input_turn_id=call.input_turn_id,
                        stimulus_revision=0,
                        response_id=call.response_id,
                        call_id=call.actual_call_id,
                        turn_id=identity["turn_id"],
                        media_session_id=str(session.media_session_id),
                    ),
                )

        await activity("TOOL_START")
        authorization = {
            "origin_session_id": str(session.session_id),
            "input_turn_id": call.input_turn_id,
            "input_order": call.input_order,
            "stimulus_revision": call.stimulus_revision,
            "response_id": call.response_id,
            "supplier_response_id": call.supplier_response_id,
            "request_id": call.request_id,
            "actual_call_id": call.actual_call_id,
        }

        async def submit(bound, values):
            worker = worker_provider()
            if worker is None:
                return {
                    "ok": False,
                    "state": "failed",
                    "operation_id": str(bound.command_id),
                    "data": {},
                    "error": {
                        "code": "BACKEND_UNAVAILABLE",
                        "message": "Background task worker is unavailable.",
                        "retryable": False,
                    },
                }
            result = await M5TaskService(pool).submit_m5_task(
                session.session_id,
                bound.command_id,
                values["task_description"],
                None,
                {
                    **authorization,
                    "input_modality": "audio",
                    "media_session_id": str(session.media_session_id),
                    "provider_item_id": identity["provider_item_id"],
                },
            )
            if not result.get("accepted"):
                return {
                    "ok": False,
                    "state": "failed",
                    "operation_id": str(bound.command_id),
                    "data": {},
                    "error": result.get("error")
                    or {
                        "code": "EXECUTION_UNKNOWN",
                        "message": "Task acceptance is unknown.",
                        "retryable": False,
                    },
                }
            worker.wake()
            return {
                "ok": True,
                "state": "accepted",
                "operation_id": str(bound.command_id),
                "data": {k: result[k] for k in ("task_id", "task_title", "task_state")},
                "error": None,
            }

        async def selection_opened(result):
            await session.emit(
                type="m5_selection_opened",
                selection={
                    "selection_id": result["selection_id"],
                    "version": result["version"],
                    "message": result["message"],
                    "state": "open",
                    "origin_session_id": str(session.session_id),
                },
            )

        if call.name == "voice_recall":
            if (
                sum(
                    c.name == "voice_recall" and c.input_turn_id == call.input_turn_id
                    for c in session.calls.values()
                )
                > 2
            ):
                raise ValueError(
                    "Voice recall exceeded the existing two-call input limit"
                )
            started = perf_counter()
            output = await VoiceRecallService(pool).execute(
                session_id=session.session_id,
                input_turn_id=UUID(call.input_turn_id),
                name=call.name,
                arguments=call.raw_arguments,
            )
            result = json.loads(output)
            success = result.get("status") not in {"error", "failed"}
            await session.emit(
                type="recall_result",
                input_turn_id=call.input_turn_id,
                result={**result, "elapsed_ms": (perf_counter() - started) * 1000},
            )
        else:
            handlers = {
                "submit": submit,
                "pend_message": pend_message_handler(
                    pool, selection_opened, await_or_closed=session._await_or_closed
                ),
            }
            handlers.update(lightweight_memory_handlers(lightweight_provider()))
            output = await execute_bound_tool(session, call, handlers=handlers)
            result = json.loads(output)
            success = result.get("ok") is True
        await history.record_tool_result(
            turn_id=identity["turn_id"],
            call_id=call.actual_call_id,
            tool_name=call.name,
            arguments=arguments,
            output=result,
            output_text=output,
            success=success,
        )
        await activity("TOOL_RESULT", success, result)
        return output

    return execute
