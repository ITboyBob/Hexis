"""Local scripted OpenAI-compatible provider and production Hexis API for BOB-108."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from collections import deque
from dataclasses import dataclass, replace
from typing import Any
from uuid import UUID, uuid4

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

import apps.hexis_api as hexis_api


class ScriptedProvider:
    def __init__(self) -> None:
        self._actions: deque[dict[str, Any]] = deque()
        self._lock = asyncio.Lock()
        self._request_count = 0

    async def replace(self, actions: list[dict[str, Any]]) -> int:
        if not actions or len(actions) > 12:
            raise ValueError("provider script must contain between 1 and 12 actions")
        checked: deque[dict[str, Any]] = deque()
        for action in actions:
            if not isinstance(action, dict) or set(action) - {"tool_call", "content"}:
                raise ValueError("provider script actions may contain tool_call or content only")
            if ("tool_call" in action) == ("content" in action):
                raise ValueError("each provider script action must select exactly one response shape")
            if "tool_call" in action:
                call = action["tool_call"]
                if not isinstance(call, dict) or not isinstance(call.get("name"), str) or not isinstance(call.get("arguments"), dict):
                    raise ValueError("scripted tool calls require a name and object arguments")
                checked.append({"tool_call": {"id": str(call.get("id") or f"bob108-{uuid4()}"), **call}})
            else:
                content = action["content"]
                if not isinstance(content, str):
                    raise ValueError("scripted response content must be text")
                checked.append({"content": content})
        async with self._lock:
            self._actions = checked
            self._request_count = 0
        return len(checked)

    async def next(self, requested_tools: set[str]) -> dict[str, Any]:
        async with self._lock:
            if not self._actions:
                raise HTTPException(status_code=409, detail="No scripted provider response is armed.")
            action = self._actions[0]
            if "tool_call" in action and action["tool_call"]["name"] not in requested_tools:
                raise HTTPException(status_code=422, detail="Scripted tool is not present in the AgentLoop request.")
            self._actions.popleft()
            self._request_count += 1
        if "tool_call" in action:
            call = action["tool_call"]
            return {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": call["id"],
                            "type": "function",
                            "function": {
                                "name": call["name"],
                                "arguments": json.dumps(call["arguments"], ensure_ascii=False),
                            },
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        return {
            "message": {"role": "assistant", "content": action["content"]},
            "finish_reason": "stop",
        }


scripted_provider = ScriptedProvider()
provider_app = FastAPI(title="BOB-108 loopback scripted provider")


@dataclass
class BackgroundToolBarrier:
    barrier_id: str
    task_a: dict[str, str]
    task_b: dict[str, str]
    entered: asyncio.Event
    release: asyncio.Event


_background_barriers: dict[str, BackgroundToolBarrier] = {}
_active_background_barrier: BackgroundToolBarrier | None = None
_background_prepare_wrapped = False


@provider_app.post("/__control/script")
async def replace_provider_script(request: Request):
    if os.environ.get("BOB108_CONTROLLED_RUNTIME") != "1":
        raise HTTPException(status_code=404)
    body = await request.json()
    actions = body.get("actions") if isinstance(body, dict) else None
    if not isinstance(actions, list):
        raise HTTPException(status_code=422, detail="actions must be a list")
    try:
        action_count = await scripted_provider.replace(actions)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"armed": True, "action_count": action_count}


@provider_app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    if os.environ.get("BOB108_CONTROLLED_RUNTIME") != "1":
        raise HTTPException(status_code=404)
    body = await request.json()
    tools = body.get("tools") if isinstance(body, dict) else None
    requested_names = {
        str(item.get("function", {}).get("name"))
        for item in tools or []
        if isinstance(item, dict) and isinstance(item.get("function"), dict)
    }
    # 潜意识的 JSON appraisal 使用同一 Provider，但不属于工具执行脚本。
    if not requested_names and body.get("response_format", {}).get("type") == "json_object":
        answer = {"message": {"role": "assistant", "content": "{}"}, "finish_reason": "stop"}
    else:
        answer = await scripted_provider.next(requested_names)
    completion_id = f"chatcmpl-bob108-{uuid4()}"
    model = str(body.get("model") or "bob108-scripted")
    if body.get("stream") is True:
        message = answer["message"]
        delta: dict[str, Any] = {"role": "assistant"}
        if message.get("tool_calls"):
            delta["tool_calls"] = [
                {"index": index, **call}
                for index, call in enumerate(message["tool_calls"])
            ]
        else:
            delta["content"] = message.get("content") or ""

        async def stream():
            for chunk_delta, finish in [(delta, None), ({}, answer["finish_reason"])]:
                chunk = {
                    "id": completion_id, "object": "chat.completion.chunk",
                    "created": 1, "model": model,
                    "choices": [{"index": 0, "delta": chunk_delta, "finish_reason": finish}],
                }
                yield "data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream")
    return JSONResponse(
        {
            "id": completion_id,
            "object": "chat.completion",
            "created": 1,
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": answer["message"],
                    "finish_reason": answer["finish_reason"],
                }
            ],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }
    )


class _BackgroundBarrierRegistry:
    """Delegate the unchanged production registry and pause one real handler result."""

    def __init__(self, registry: Any, barrier: BackgroundToolBarrier) -> None:
        self._registry = registry
        self._barrier = barrier
        self.pool = registry.pool

    def __getattr__(self, name: str) -> Any:
        return getattr(self._registry, name)

    async def execute(self, tool_name: str, arguments: dict[str, Any], context: Any):
        result = await self._registry.execute(tool_name, arguments, context)
        if tool_name == "read_file" and not self._barrier.entered.is_set():
            self._barrier.entered.set()
            await self._barrier.release.wait()
        return result


def _install_background_prepare_barrier() -> None:
    global _background_prepare_wrapped
    if _background_prepare_wrapped:
        return
    from services import m5_task_worker
    from services.background_preparation import prepare_background_turn

    async def prepare_with_barrier(*args: Any, **kwargs: Any):
        prepared = await prepare_background_turn(*args, **kwargs)
        barrier = _active_background_barrier
        if barrier is None or barrier.release.is_set() or str(prepared.session_id) != barrier.task_a["task_session_id"]:
            return prepared
        return replace(prepared, registry=_BackgroundBarrierRegistry(prepared.registry, barrier))

    m5_task_worker.prepare_background_turn = prepare_with_barrier
    _background_prepare_wrapped = True


async def _submit_controlled_task(origin_session_id: UUID, *, title: str, text: str) -> dict[str, str]:
    from services.m5_task_service import M5TaskService

    accepted = await M5TaskService(hexis_api._pool).submit_m5_task(
        origin_session_id,
        uuid4(),
        title,
        text,
        {
            "origin_session_id": str(origin_session_id),
            "input_turn_id": str(uuid4()),
            "input_order": 1,
            "stimulus_revision": 0,
            "response_id": f"bob108-response-{uuid4()}",
            "supplier_response_id": f"bob108-supplier-{uuid4()}",
            "request_id": f"bob108-request-{uuid4()}",
            "actual_call_id": f"bob108-submit-{uuid4()}",
        },
    )
    return {
        "task_id": str(accepted["task_id"]),
        "task_session_id": str(accepted["task_session_id"]),
        "work_execution_id": str(accepted["work_execution_id"]),
    }


def _install_controlled_api_routes() -> None:
    if os.environ.get("BOB108_CONTROLLED_RUNTIME") != "1":
        raise RuntimeError("controlled API routes require BOB108_CONTROLLED_RUNTIME=1")
    if any(getattr(route, "path", None) == "/__test__/tool-activity/identity" for route in hexis_api.app.routes):
        return

    async def identity():
        if os.environ.get("BOB108_CONTROLLED_RUNTIME") != "1" or hexis_api._pool is None:
            raise HTTPException(status_code=404)
        async with hexis_api._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT current_database() AS database, inet_server_addr()::text AS address, inet_server_port() AS port, (SELECT system_identifier FROM pg_control_system())::text AS system_identifier"
            )
        return {
            "run_id": os.environ.get("BOB108_RUN_ID"),
            "database_identity": dict(row),
            "task_worker_ready": bool(hexis_api._m5_task_worker and hexis_api._m5_task_worker.running),
            "lightweight_worker_ready": bool(hexis_api._m5_lightweight_executor and hexis_api._m5_lightweight_executor.running),
        }

    async def script(request: Request):
        if os.environ.get("BOB108_CONTROLLED_RUNTIME") != "1":
            raise HTTPException(status_code=404)
        body = await request.json()
        actions = body.get("actions") if isinstance(body, dict) else None
        if not isinstance(actions, list):
            raise HTTPException(status_code=422, detail="actions must be a list")
        try:
            action_count = await scripted_provider.replace(actions)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"armed": True, "action_count": action_count}

    async def voice_recall(request: Request):
        if os.environ.get("BOB108_CONTROLLED_RUNTIME") != "1" or hexis_api._pool is None:
            raise HTTPException(status_code=404)
        body = await request.json()
        try:
            session_id = UUID(str(body.get("session_id")))
        except (AttributeError, TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail="session_id must be a UUID") from exc
        async with hexis_api._pool.acquire() as conn:
            exists = await conn.fetchval("SELECT EXISTS (SELECT 1 FROM chat_sessions WHERE id=$1)", session_id)
        if not exists:
            raise HTTPException(status_code=404, detail="The selected owned chat session does not exist.")

        from services.voice_realtime_session import VoiceRealtimeSession
        from services.voice_recall import VoiceRecallService

        class LocalToolClient:
            async def close(self):
                return None

        media_session_id = f"bob108-scripted-voice-{uuid4()}"
        input_turn_id = str(uuid4())
        response_id = f"bob108-response-{uuid4()}"
        call_id = f"bob108-voice-call-{uuid4()}"
        session = VoiceRealtimeSession(
            session_id=session_id,
            media_session_id=media_session_id,
            client=LocalToolClient(),
        )
        hexis_api._bind_voice_behavior_event_sink(session, hexis_api._pool)
        arguments = {"query": "Hexis"}
        await hexis_api._emit_voice_tool_activity(
            session,
            event_type="TOOL_START",
            call_id=call_id,
            response_id=response_id,
            input_turn_id=input_turn_id,
            stimulus_revision=0,
            tool_name="voice_recall",
            arguments=arguments,
        )
        output_text = await VoiceRecallService(hexis_api._pool).execute(
            session_id=session_id,
            input_turn_id=UUID(input_turn_id),
            name="voice_recall",
            arguments=json.dumps(arguments, ensure_ascii=False),
        )
        output = json.loads(output_text)
        success = output.get("status") not in {"error", "failed"}
        error_payload = output.get("error")
        error = error_payload.get("message") if isinstance(error_payload, dict) else None
        await hexis_api._emit_voice_tool_activity(
            session,
            event_type="TOOL_RESULT",
            call_id=call_id,
            response_id=response_id,
            input_turn_id=input_turn_id,
            stimulus_revision=0,
            tool_name="voice_recall",
            arguments=arguments,
            success=success,
            output=output,
            error=error if isinstance(error, str) else None,
        )
        from services.behavior_events import query_behavior_events

        events = await query_behavior_events(
            hexis_api._pool,
            session_id=session_id,
            event_type="TOOL_START",
            call_id=call_id,
            limit=5,
        )
        result_events = await query_behavior_events(
            hexis_api._pool,
            session_id=session_id,
            event_type="TOOL_RESULT",
            call_id=call_id,
            limit=5,
        )
        return {
            "session_id": str(session_id),
            "media_session_id": media_session_id,
            "response_id": response_id,
            "call_id": call_id,
            "request_event_id": events["events"][0]["event_id"] if events.get("events") else None,
            "result_event_id": result_events["events"][0]["event_id"] if result_events.get("events") else None,
            "result_status": output.get("status"),
        }

    async def background_start(request: Request):
        global _active_background_barrier
        if os.environ.get("BOB108_CONTROLLED_RUNTIME") != "1" or hexis_api._pool is None:
            raise HTTPException(status_code=404)
        if _active_background_barrier is not None and not _active_background_barrier.release.is_set():
            raise HTTPException(status_code=409, detail="A controlled background barrier is already active.")
        body = await request.json()
        try:
            origin_session_id = UUID(str(body.get("origin_session_id")))
        except (AttributeError, TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail="origin_session_id must be a UUID") from exc
        async with hexis_api._pool.acquire() as conn:
            exists = await conn.fetchval("SELECT EXISTS (SELECT 1 FROM chat_sessions WHERE id=$1)", origin_session_id)
        if not exists:
            raise HTTPException(status_code=404, detail="The selected owned origin session does not exist.")
        worker = hexis_api._m5_task_worker
        if worker is None or not worker.running:
            raise HTTPException(status_code=503, detail="The controlled M5 task worker is not ready.")

        background_readme = os.path.join(os.path.expanduser("~"), "README.md")
        try:
            await scripted_provider.replace([
                {"tool_call": {"id": f"bob108-read-file-{uuid4()}", "name": "read_file", "arguments": {"path": background_readme}}},
                {"content": "受控 README 工具调用已完成。"},
                {"content": "任务 B 不执行工具，仅用于会话隔离检查。"},
            ])
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        task_a = await _submit_controlled_task(
            origin_session_id,
            title="BOB-108 受控 README 检查 A",
            text="请使用 read_file 检查 README.md 并总结。",
        )
        task_b = await _submit_controlled_task(
            origin_session_id,
            title="BOB-108 隔离检查 B",
            text="请用一句话说明任务 B 已就绪。",
        )
        barrier = BackgroundToolBarrier(
            barrier_id=str(uuid4()),
            task_a=task_a,
            task_b=task_b,
            entered=asyncio.Event(),
            release=asyncio.Event(),
        )
        _active_background_barrier = barrier
        _background_barriers[barrier.barrier_id] = barrier
        _install_background_prepare_barrier()
        worker.wake()
        return {
            "barrier_id": barrier.barrier_id,
            "origin_session_id": str(origin_session_id),
            "task_a": task_a,
            "task_b": task_b,
            "scripted_actions": 3,
        }

    async def background_status(request: Request):
        if os.environ.get("BOB108_CONTROLLED_RUNTIME") != "1" or hexis_api._pool is None:
            raise HTTPException(status_code=404)
        body = await request.json()
        barrier_id = body.get("barrier_id") if isinstance(body, dict) else None
        barrier = _background_barriers.get(str(barrier_id))
        if barrier is None:
            raise HTTPException(status_code=404, detail="Unknown controlled background barrier.")
        states: dict[str, Any] = {}
        events: list[dict[str, Any]] = []
        async with hexis_api._pool.acquire() as conn:
            for name, task in (("task_a", barrier.task_a), ("task_b", barrier.task_b)):
                row = await conn.fetchrow(
                    "SELECT t.product_state,w.status AS work_status FROM m5_tasks t "
                    "JOIN m5_work_rounds w ON w.id=t.current_work_execution_id WHERE t.id=$1::uuid",
                    task["task_id"],
                )
                states[name] = dict(row) if row is not None else None
            events = [dict(row) for row in await conn.fetch(
                "SELECT event_type,event_id,session_id,task_id,call_id,payload->>'tool_name' AS tool_name,"
                "payload->>'execution_id' AS execution_id,recorded_at "
                "FROM behavior_events WHERE task_id=$1::text AND event_type IN ('TOOL_START','TOOL_RESULT') "
                "ORDER BY recorded_at,producer_seq",
                barrier.task_a["task_id"],
            )]
        return {
            "barrier_id": barrier.barrier_id,
            "entered": barrier.entered.is_set(),
            "released": barrier.release.is_set(),
            "task_a": barrier.task_a,
            "task_b": barrier.task_b,
            "states": states,
            "events": [{**event, "recorded_at": event["recorded_at"].isoformat()} for event in events],
        }

    async def background_release(request: Request):
        if os.environ.get("BOB108_CONTROLLED_RUNTIME") != "1":
            raise HTTPException(status_code=404)
        body = await request.json()
        barrier_id = body.get("barrier_id") if isinstance(body, dict) else None
        barrier = _background_barriers.get(str(barrier_id))
        if barrier is None:
            raise HTTPException(status_code=404, detail="Unknown controlled background barrier.")
        barrier.release.set()
        return {"barrier_id": barrier.barrier_id, "released": True}

    hexis_api.app.add_api_route("/__test__/tool-activity/identity", identity, methods=["GET"])
    hexis_api.app.add_api_route("/__test__/tool-activity/script", script, methods=["POST"])
    hexis_api.app.add_api_route("/__test__/tool-activity/voice-recall", voice_recall, methods=["POST"])
    hexis_api.app.add_api_route("/__test__/tool-activity/background/start", background_start, methods=["POST"])
    hexis_api.app.add_api_route("/__test__/tool-activity/background/status", background_status, methods=["POST"])
    hexis_api.app.add_api_route("/__test__/tool-activity/background/release", background_release, methods=["POST"])


async def _serve(run_dir: str) -> None:
    if os.environ.get("BOB108_CONTROLLED_RUNTIME") != "1":
        raise RuntimeError("controlled runtime process requires BOB108_CONTROLLED_RUNTIME=1")
    _install_controlled_api_routes()
    api_port = int(os.environ["BOB108_API_PORT"])
    provider_port = int(os.environ["BOB108_PROVIDER_PORT"])
    api_config = uvicorn.Config(hexis_api.app, host="127.0.0.1", port=api_port, log_level="info")
    provider_config = uvicorn.Config(provider_app, host="127.0.0.1", port=provider_port, log_level="warning")
    api_server = uvicorn.Server(api_config)
    provider_server = uvicorn.Server(provider_config)
    await asyncio.gather(api_server.serve(), provider_server.serve())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True)
    args = parser.parse_args()
    asyncio.run(_serve(args.run_dir))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
