"""Long-lived realtime voice session ownership and event routing."""

from __future__ import annotations

import asyncio
import time
from typing import Any
from uuid import UUID, uuid4

from services.m5_tool_bridge import BoundToolCall
from services.qwen_realtime_protocol import ProtocolEvent
from services.voice_input_authorization import (
    CaptureRange,
    FinalTranscript,
    InputAuthorizationService,
    InputCommitReceipt,
    InputIdentity,
)
from services.voice_instructions import eligible_gate_emotion, render_per_turn
from services.voice_output_arbiter import OutputClaim, VoiceOutputArbiter
from services.voice_recall import VOICE_RECALL_TOOL
from services.m5_tool_contracts import realtime_tool_definitions


class VoiceRealtimeSessionError(RuntimeError):
    """The realtime session violated a readiness or routing invariant."""


class VoiceTranscriptUnavailable(VoiceRealtimeSessionError):
    """The cloud committed audio but returned no usable final transcript."""


class ActiveVoiceSessionExists(RuntimeError):
    """A chat already has one active realtime media session."""


class VoiceRealtimeSession:
    """One server-owned cloud session with a single receive dispatcher."""

    def __init__(
        self,
        *,
        session_id: UUID,
        media_session_id: str,
        client: Any,
        queue_limit: int = 1000,
        on_input_authorized: Any | None = None,
        output_arbiter: VoiceOutputArbiter | None = None,
    ) -> None:
        if not isinstance(session_id, UUID):
            raise ValueError("session_id must be a UUID")
        if not isinstance(media_session_id, str) or not media_session_id.strip():
            raise ValueError("media_session_id must be a non-empty string")
        if queue_limit <= 0:
            raise ValueError("queue_limit must be positive")
        self.session_id = session_id
        self.media_session_id = media_session_id
        self.client = client
        self._on_input_authorized = on_input_authorized
        self._input_gate_results: dict[str, Any] = {}
        self._output_arbiter = output_arbiter or VoiceOutputArbiter(session_id=session_id)
        self._response_claims: dict[str, OutputClaim] = {}
        self._playback_ids: dict[str, str] = {}
        self._playback_state: dict[str, dict[str, Any]] = {}
        self.queue_limit = queue_limit
        self.cloud_session_id: str | None = None
        self.tool_surface = "m4"
        self.stable_instructions = ""
        self._ready = False
        self._closed = False
        self._closed_event = asyncio.Event()
        self._committed_events: asyncio.Queue[ProtocolEvent] = asyncio.Queue(
            maxsize=queue_limit
        )
        self._final_events: dict[str, ProtocolEvent] = {}
        self._final_futures: dict[str, asyncio.Future[ProtocolEvent]] = {}
        self._response_queues: dict[str, asyncio.Queue[ProtocolEvent]] = {}
        self._responses_idle = asyncio.Event()
        self._responses_idle.set()
        self._instruction_turn_lock = asyncio.Lock()
        self._supplier_response_map: dict[str, str] = {}
        self._authorized_input_ids: set[str] = set()
        self._response_input_ids: dict[str, str] = {}
        self._voice_tool_call_counts: dict[str, int] = {}
        self._used_voice_call_ids: set[str] = set()
        self._bound_voice_calls: dict[str, BoundToolCall] = {}
        self._voice_call_inputs: dict[str, str] = {}
        self._completed_voice_call_ids: set[str] = set()
        self._invalidated_response_ids: set[str] = set()
        self._voice_turns: dict[str, dict[str, Any]] = {}
        self._turn_completion_lock = asyncio.Lock()
        self._background_tasks: set[asyncio.Task[Any]] = set()
        self._input_authorization = InputAuthorizationService()
        self._input_lock = asyncio.Lock()
        self._input_order = 0
        self._event_sink: Any | None = None
        self._behavior_event_dispatch: Any | None = None
        self._response_dispatch: Any | None = None
        # Producer sequence 0 is reserved for voice_session_ready.
        self._event_seq = 1

    @property
    def ready(self) -> bool:
        return self._ready and not self._closed

    @property
    def active_response_id(self) -> str | None:
        return self._output_arbiter.active_response_id

    def response_is_invalidated(self, response_id: str) -> bool:
        state = self._playback_state.get(response_id)
        return response_id in self._invalidated_response_ids or bool(
            state and state.get("invalidated")
        )

    def local_response_id(self, supplier_response_id: str) -> str | None:
        """Resolve the Qwen response identity to its local output claim."""
        if not isinstance(supplier_response_id, str) or not supplier_response_id.strip():
            return None
        mapped = self._supplier_response_map.get(supplier_response_id)
        if mapped is not None:
            return mapped
        return supplier_response_id if supplier_response_id in self._response_queues else None

    async def invalidate_foreground_output(self) -> str | None:
        """Fence the active output locally before asking Qwen to cancel it."""
        response_id = self._output_arbiter.active_response_id
        if response_id is None:
            return None
        state = self._playback_state.get(response_id)
        if state is None:
            raise VoiceRealtimeSessionError("foreground output has no playback state")
        state["invalidated"] = True
        self._invalidated_response_ids.add(response_id)
        claim = self._response_claims.pop(response_id, None)
        if claim is not None:
            await self._output_arbiter.release(response_id, self.media_session_id)
        await self.client.cancel_response()
        return response_id

    async def start(self, instructions: str, *, tool_surface: str = "m4") -> str:
        if self._closed:
            raise VoiceRealtimeSessionError("realtime session is closed")
        if self._ready:
            raise VoiceRealtimeSessionError("realtime session already started")
        if tool_surface not in {"m4", "m5"}:
            raise VoiceRealtimeSessionError("unsupported realtime tool surface")
        if tool_surface == "m5":
            from services.voice_instructions import load_m5_instructions

            if instructions != load_m5_instructions().stable:
                raise VoiceRealtimeSessionError(
                    "M5 session must use the explicitly selected M5 stable instructions"
                )
            m5_recall = {"type": "function", **VOICE_RECALL_TOOL["function"]}
            tools = [m5_recall, *realtime_tool_definitions()]
        else:
            tools = [VOICE_RECALL_TOOL]
        cloud_session_id = await self.client.connect(
            instructions, tools=tools
        )
        if not isinstance(cloud_session_id, str) or not cloud_session_id.strip():
            raise VoiceRealtimeSessionError(
                "session.updated did not provide a usable cloud session id"
            )
        self.cloud_session_id = cloud_session_id
        self.tool_surface = tool_surface
        self.stable_instructions = instructions
        self._ready = True
        return cloud_session_id

    def subscribe_response(self, response_id: str) -> asyncio.Queue[ProtocolEvent]:
        if not isinstance(response_id, str) or not response_id.strip():
            raise ValueError("response_id must be a non-empty string")
        if response_id in self._response_queues:
            raise VoiceRealtimeSessionError(
                f"response {response_id} already has a subscriber"
            )
        queue: asyncio.Queue[ProtocolEvent] = asyncio.Queue(maxsize=self.queue_limit)
        self._response_queues[response_id] = queue
        return queue

    async def wait_for_input_committed(self) -> ProtocolEvent:
        return await self._await_or_closed(self._committed_events.get())

    async def _await_or_closed(self, awaitable: Any) -> Any:
        result_task = asyncio.ensure_future(awaitable)
        closed_task = asyncio.create_task(self._closed_event.wait())
        try:
            if self._closed_event.is_set():
                raise VoiceRealtimeSessionError("realtime session is closed")
            done, _pending = await asyncio.wait(
                (result_task, closed_task), return_when=asyncio.FIRST_COMPLETED
            )
            if result_task in done:
                return result_task.result()
            raise VoiceRealtimeSessionError("realtime session is closed")
        finally:
            for task in (result_task, closed_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(result_task, closed_task, return_exceptions=True)

    def response_instructions(self, input_turn_id: str) -> tuple[str, dict[str, Any]]:
        """Freeze the authorized input's hydrated context and eligible C label."""
        gate = self._input_gate_results.get(input_turn_id)
        if gate is None or not gate.released or not gate.context_ready:
            raise VoiceRealtimeSessionError("input context gate is not ready")
        identity = dict(gate.identity)
        if identity.get("session_id") != str(self.session_id):
            raise VoiceRealtimeSessionError("input context belongs to another session")
        hydration = gate.hydration
        if hydration is None:
            raise VoiceRealtimeSessionError("input hydration result is unavailable")
        label, eligible = eligible_gate_emotion(gate, identity)
        from services.voice_instructions import load_m4_instructions, load_m5_instructions

        instruction_bundle = (
            load_m5_instructions()
            if self.tool_surface == "m5"
            else load_m4_instructions()
        )

        rendered = render_per_turn(
            instruction_bundle,
            now=hydration.temporal_context,
            persona=hydration.persona_context,
            continuity=hydration.continuity_context,
            memory=hydration.memory_context,
            emotion=label,
            emotion_eligible=eligible,
        )
        return rendered.text, {
            "input_turn_id": input_turn_id,
            "stable_instructions_hash": hydration.instructions_hash,
            "per_turn_instructions_hash": rendered.sha256,
            "per_turn_token_count": rendered.token_count,
            "tokenizer": rendered.tokenizer,
            "tokenizer_is_model_exact": rendered.tokenizer_is_model_exact,
            "emotion_included": eligible,
        }

    async def start_authorized_response(self, input_turn_id: str) -> dict[str, Any]:
        """Claim output and send one response for the already-authorized input."""
        if not self.ready:
            raise VoiceRealtimeSessionError("realtime session is not ready")
        async with self._instruction_turn_lock:
            await self._responses_idle.wait()
            instructions, facts = self.response_instructions(input_turn_id)
            await self.client.update_session_instructions(instructions)
            response_id = str(uuid4())
            claim = await self._output_arbiter.claim(response_id, self.media_session_id)
            playback_id = str(uuid4())
            queue = self.subscribe_response(response_id)
            self._responses_idle.clear()
            try:
                await self.client.create_first_response(response_id)
            except Exception:
                self._response_queues.pop(response_id, None)
                if not self._response_queues:
                    self._responses_idle.set()
                await self._output_arbiter.release(response_id, self.media_session_id)
                raise
            self._response_claims[response_id] = claim
            self._authorized_input_ids.add(input_turn_id)
            self._response_input_ids[response_id] = input_turn_id
            self._voice_tool_call_counts.setdefault(input_turn_id, 0)
            self._playback_ids[response_id] = playback_id
            self._playback_state[response_id] = {
                "output_epoch": claim.output_epoch,
                "playback_id": playback_id,
                "scheduled_frames": 0,
                "scheduled_ms": 0,
                "opened": False,
                "terminal": False,
                "last_seq": 0,
                "receipts": {},
                "played_ms": 0,
                "invalidated": False,
            }
            turn = self._voice_turns.get(input_turn_id)
            if turn is not None:
                turn["response_ids"].append(response_id)
            return {
                "response_id": response_id,
                "playback_id": playback_id,
                "output_epoch": claim.output_epoch,
                "events": queue,
                "instruction_facts": facts,
                "input_turn_id": input_turn_id,
                "turn_id": str(turn["turn_id"]) if turn is not None else None,
            }

    def bind_voice_turn(
        self,
        *,
        input_turn_id: str,
        input_order: int,
        user_text: str,
        context_version: str,
        turn_id: UUID,
    ) -> None:
        gate = self._input_gate_results.get(input_turn_id)
        if gate is None or not gate.released or not gate.context_ready:
            raise VoiceRealtimeSessionError("cannot bind a turn before input authorization and hydration")
        identity = gate.identity
        if (
            identity.get("session_id") != str(self.session_id)
            or identity.get("input_turn_id") != input_turn_id
            or identity.get("input_order") != input_order
        ):
            raise VoiceRealtimeSessionError("voice turn identity does not match its authorized input")
        self._voice_turns[input_turn_id] = {
            "turn_id": turn_id,
            "input_order": input_order,
            "stimulus_revision": identity.get("stimulus_revision", 0),
            "user_text": user_text,
            "context_version": context_version,
            "response_ids": [],
            "playback_ids": [],
            "generated_text": {},
            "generation_done": False,
            "terminal_receipts": {},
        }

    def record_generation_text(
        self, input_turn_id: str, response_id: str, text: str, *, final: bool = False
    ) -> None:
        turn = self._voice_turns.get(input_turn_id)
        if turn is None:
            return
        generated = turn["generated_text"].setdefault(
            response_id, {"text": "", "final": False}
        )
        if final:
            generated["text"] = text
            generated["final"] = True
        elif not generated["final"]:
            generated["text"] += text

    async def mark_generation_done(
        self, input_turn_id: str, response_id: str, *, final_for_turn: bool = True
    ) -> None:
        turn = self._voice_turns.get(input_turn_id)
        if turn is not None:
            turn["generation_done"] = final_for_turn
            if response_id not in turn["response_ids"]:
                turn["response_ids"].append(response_id)
        state = self._playback_state.get(response_id)
        if state is not None:
            state["generation_done"] = True
        await self._finalize_response_if_complete(response_id)

    def record_playback_terminal(
        self,
        input_turn_id: str,
        response_id: str,
        playback_id: str,
        scope: str,
        played_ms: int,
        output_epoch: int,
    ) -> None:
        turn = self._voice_turns.get(input_turn_id)
        if turn is None:
            return
        terminal = {
            "playback_id": playback_id,
            "response_id": response_id,
            "scope": scope,
            "played_ms": played_ms,
            "output_epoch": output_epoch,
        }
        turn["terminal_receipts"][response_id] = terminal
        if not any(item["playback_id"] == playback_id for item in turn["playback_ids"]):
            turn["playback_ids"].append(terminal)

    def voice_turn_snapshot(self, input_turn_id: str) -> dict[str, Any] | None:
        turn = self._voice_turns.get(input_turn_id)
        if turn is None:
            return None
        final_response_id = turn["response_ids"][-1] if turn["response_ids"] else ""
        playback_scope = turn["terminal_receipts"].get(final_response_id, {}).get("scope", "unknown")
        played_texts = [
            {**turn["terminal_receipts"][response_id], "response_id": response_id, "text": text}
            for response_id in turn["response_ids"]
            if turn["terminal_receipts"].get(response_id, {}).get("scope") == "complete"
            and (text := turn["generated_text"].get(response_id, {}).get("text", "").strip())
        ]
        assistant_text = turn["generated_text"].get(final_response_id, {}).get("text", "")
        if playback_scope == "complete":
            assistant_text = "\n".join(item["text"] for item in played_texts)
        return {
            **turn,
            "assistant_text": assistant_text,
            "playback_scope": playback_scope,
            "played_response_texts": played_texts,
        }

    async def claim_voice_turn_completion(
        self, input_turn_id: str
    ) -> dict[str, Any] | None:
        async with self._turn_completion_lock:
            turn = self._voice_turns.get(input_turn_id)
            if turn is None or not turn["generation_done"]:
                return None
            if turn.get("completion_inflight") or turn.get("completed"):
                return None
            final_response_id = turn["response_ids"][-1] if turn["response_ids"] else ""
            terminal = turn["terminal_receipts"].get(final_response_id)
            if terminal is None:
                return None
            turn["completion_inflight"] = True
            snapshot = self.voice_turn_snapshot(input_turn_id)
            assert snapshot is not None
            return {
                **snapshot,
                "input_turn_id": input_turn_id,
                "final_response_id": final_response_id,
                "playback_scope": terminal["scope"],
                "terminal": terminal,
            }

    async def finish_voice_turn_completion(
        self, input_turn_id: str, *, success: bool
    ) -> None:
        async with self._turn_completion_lock:
            turn = self._voice_turns.get(input_turn_id)
            if turn is None:
                return
            turn["completion_inflight"] = False
            if success:
                turn["completed"] = True

    def authorize_voice_tool_call(
        self,
        *,
        response_id: str,
        call_id: str,
        name: str,
        arguments: str,
        supplier_response_id: str | None = None,
        request_id: str | None = None,
    ) -> BoundToolCall:
        if not isinstance(call_id, str) or not call_id.strip():
            raise VoiceRealtimeSessionError("voice tool call requires a real call id")
        if not isinstance(name, str) or not name.strip():
            raise VoiceRealtimeSessionError("voice tool name is missing")
        if not isinstance(arguments, str):
            raise VoiceRealtimeSessionError("voice tool arguments are missing")
        if not isinstance(response_id, str) or not response_id.strip():
            raise VoiceRealtimeSessionError("voice tool call requires a response id")
        supplier_response_id = supplier_response_id or response_id
        previous = self._bound_voice_calls.get(call_id)
        if previous is not None:
            if (
                previous.name == name
                and previous.raw_arguments == arguments
                and previous.response_id == response_id
                and previous.supplier_response_id == supplier_response_id
                and previous.tool_surface == self.tool_surface
            ):
                return previous
            raise VoiceRealtimeSessionError("tool call id conflicts with its original binding")

        input_turn_id = self._response_input_ids.get(response_id)
        if self.response_is_invalidated(response_id):
            raise VoiceRealtimeSessionError("invalidated response cannot authorize a voice tool call")
        if (
            not self.ready
            or input_turn_id is None
            or input_turn_id not in self._authorized_input_ids
        ):
            raise VoiceRealtimeSessionError("voice tool call is not bound to an authorized input")
        gate = self._input_gate_results.get(input_turn_id)
        turn = self._voice_turns.get(input_turn_id)
        if gate is None or turn is None or not gate.released or not gate.context_ready:
            raise VoiceRealtimeSessionError("voice tool call has no completed input authorization")
        identity = gate.identity
        if (
            identity.get("session_id") != str(self.session_id)
            or identity.get("input_turn_id") != input_turn_id
            or identity.get("input_order") != turn.get("input_order")
            or identity.get("stimulus_revision") != turn.get("stimulus_revision")
        ):
            raise VoiceRealtimeSessionError("voice tool input revision or identity is stale")
        if name == "voice_recall":
            count = self._voice_tool_call_counts.get(input_turn_id, 0)
            if count >= 2:
                raise VoiceRealtimeSessionError("voice recall exceeded the two-call input limit")
            self._voice_tool_call_counts[input_turn_id] = count + 1
        bound = BoundToolCall(
            actual_call_id=call_id,
            command_id=uuid4(),
            name=name,
            raw_arguments=arguments,
            origin_session_id=self.session_id,
            input_turn_id=input_turn_id,
            input_order=int(turn["input_order"]),
            stimulus_revision=int(turn["stimulus_revision"]),
            response_id=response_id,
            supplier_response_id=supplier_response_id,
            request_id=request_id,
            tool_surface=self.tool_surface,
        )
        self._used_voice_call_ids.add(call_id)
        self._voice_call_inputs[call_id] = input_turn_id
        self._bound_voice_calls[call_id] = bound
        return bound

    def authorize_voice_recall_call(
        self,
        *,
        response_id: str,
        call_id: str,
        name: str,
        arguments: str,
    ) -> str:
        """M4 compatibility wrapper for the generalized voice tool boundary."""
        if name != "voice_recall":
            raise VoiceRealtimeSessionError("voice recall call is not bound to an authorized input")
        return self.authorize_voice_tool_call(
            response_id=response_id,
            call_id=call_id,
            name=name,
            arguments=arguments,
        ).input_turn_id

    def validate_voice_tool_call(self, call: BoundToolCall) -> None:
        if (
            not self.ready
            or call.origin_session_id != self.session_id
            or call.input_turn_id not in self._authorized_input_ids
            or self._bound_voice_calls.get(call.actual_call_id) != call
            or call.actual_call_id in self._completed_voice_call_ids
            or self.response_is_invalidated(call.response_id)
        ):
            raise VoiceRealtimeSessionError("voice tool call is no longer authorized")
        gate = self._input_gate_results.get(call.input_turn_id)
        turn = self._voice_turns.get(call.input_turn_id)
        if (
            gate is None
            or turn is None
            or not gate.released
            or not gate.context_ready
            or gate.identity.get("session_id") != str(call.origin_session_id)
            or gate.identity.get("input_turn_id") != call.input_turn_id
            or gate.identity.get("input_order") != call.input_order
            or gate.identity.get("stimulus_revision") != call.stimulus_revision
            or turn.get("input_order") != call.input_order
            or turn.get("stimulus_revision") != call.stimulus_revision
        ):
            raise VoiceRealtimeSessionError("voice tool call input identity is stale")

    async def start_voice_recall_followup(
        self,
        *,
        input_turn_id: str,
        call_id: str,
        tool_output: str,
    ) -> dict[str, Any]:
        return await self.start_voice_tool_followup(
            input_turn_id=input_turn_id,
            outputs=[(call_id, tool_output)],
        )

    async def start_voice_tool_followup(
        self,
        *,
        input_turn_id: str,
        outputs: list[tuple[str, str]],
    ) -> dict[str, Any]:
        if not self.ready or input_turn_id not in self._authorized_input_ids or not outputs:
            raise VoiceRealtimeSessionError("voice tool follow-up has no authorized input")
        call_ids = [call_id for call_id, _ in outputs]
        if len(set(call_ids)) != len(call_ids):
            raise VoiceRealtimeSessionError("voice tool follow-up contains duplicate call ids")
        for call_id, tool_output in outputs:
            bound = self._bound_voice_calls.get(call_id)
            if (
                bound is None
                or bound.input_turn_id != input_turn_id
                or self._voice_call_inputs.get(call_id) != input_turn_id
                or call_id in self._completed_voice_call_ids
                or not isinstance(tool_output, str)
            ):
                raise VoiceRealtimeSessionError("voice tool follow-up has no authorized call")
            self.validate_voice_tool_call(bound)
        async with self._instruction_turn_lock:
            await self._await_or_closed(self._responses_idle.wait())
            instructions, facts = self.response_instructions(input_turn_id)
            for call_id, tool_output in outputs:
                self.validate_voice_tool_call(self._bound_voice_calls[call_id])
                await self.client.send_function_call_output(call_id, tool_output)
            await self.client.update_session_instructions(instructions)
            response_id = str(uuid4())
            claim = await self._output_arbiter.claim(response_id, self.media_session_id)
            playback_id = str(uuid4())
            queue = self.subscribe_response(response_id)
            self._responses_idle.clear()
            try:
                await self.client.create_first_response(response_id)
            except Exception:
                self._response_queues.pop(response_id, None)
                if not self._response_queues:
                    self._responses_idle.set()
                await self._output_arbiter.release(response_id, self.media_session_id)
                raise
            self._completed_voice_call_ids.update(call_ids)
            self._response_claims[response_id] = claim
            self._playback_ids[response_id] = playback_id
            self._playback_state[response_id] = {
                "output_epoch": claim.output_epoch,
                "playback_id": playback_id,
                "scheduled_frames": 0,
                "scheduled_ms": 0,
                "opened": False,
                "terminal": False,
                "last_seq": 0,
                "receipts": {},
                "played_ms": 0,
                "invalidated": False,
            }
            self._response_input_ids[response_id] = input_turn_id
            turn = self._voice_turns.get(input_turn_id)
            if turn is not None:
                turn["response_ids"].append(response_id)
            return {
                "response_id": response_id,
                "playback_id": playback_id,
                "output_epoch": claim.output_epoch,
                "events": queue,
                "instruction_facts": facts,
                "input_turn_id": input_turn_id,
                "turn_id": str(turn["turn_id"]) if turn is not None else None,
            }

    async def release_response(self, response_id: str) -> None:
        claim = self._response_claims.pop(response_id, None)
        self._playback_ids.pop(response_id, None)
        self._response_queues.pop(response_id, None)
        if not self._response_queues:
            self._responses_idle.set()
        self._playback_state.pop(response_id, None)
        self._response_input_ids.pop(response_id, None)
        for supplier_id, local_id in list(self._supplier_response_map.items()):
            if local_id == response_id:
                del self._supplier_response_map[supplier_id]
        if claim is not None:
            await self._output_arbiter.release(response_id, self.media_session_id)

    async def _finalize_response_if_complete(self, response_id: str) -> None:
        state = self._playback_state.get(response_id)
        if (
            state is not None
            and state.get("generation_done")
            and state.get("playback_finished")
            and state["terminal"]
        ):
            await self.release_response(response_id)

    async def finish_response_playback(self, response_id: str) -> None:
        state = self._playback_state.get(response_id)
        if state is not None:
            state["playback_finished"] = True
        await self._finalize_response_if_complete(response_id)

    def record_output_chunk(self, response_id: str, pcm_bytes: bytes) -> None:
        state = self._playback_state.get(response_id)
        if state is None:
            raise VoiceRealtimeSessionError("output chunk belongs to an unclaimed response")
        if state.get("invalidated"):
            raise VoiceRealtimeSessionError("output chunk belongs to an invalidated response")
        if not isinstance(pcm_bytes, bytes) or not pcm_bytes or len(pcm_bytes) % 2:
            raise VoiceRealtimeSessionError("output PCM must contain complete mono s16le frames")
        state["scheduled_frames"] += len(pcm_bytes) // 2
        state["scheduled_ms"] = state["scheduled_frames"] * 1000 // 24_000

    def response_has_output(self, response_id: str) -> bool:
        state = self._playback_state.get(response_id)
        return bool(state and state["scheduled_frames"] > 0)

    def playback_snapshot(self, response_id: str) -> dict[str, int] | None:
        state = self._playback_state.get(response_id)
        if state is None:
            return None
        return {
            "scheduled_ms": state["scheduled_ms"],
            "played_ms": state["played_ms"],
            "last_seq": state["last_seq"],
        }

    def next_playback_producer_seq(self, response_id: str) -> int | None:
        state = self._playback_state.get(response_id)
        return state["last_seq"] + 1 if state is not None else None

    def accept_playback_receipt(self, request: Any) -> bool:
        """Bind playback lifecycle reports to server-issued identities and PCM bounds."""
        state = self._playback_state.get(request.response_id)
        claim = self._response_claims.get(request.response_id)
        playback_id = self._playback_ids.get(request.response_id)
        if state is None or playback_id is None:
            raise VoiceRealtimeSessionError("playback response is not active")
        if state.get("invalidated") and request.action != "interrupt":
            raise VoiceRealtimeSessionError("invalidated playback accepts only its terminal interrupt receipt")
        if (
            request.session_id != self.session_id
            or request.media_session_id != self.media_session_id
            or request.playback_id != playback_id
            or request.output_epoch != state["output_epoch"]
            or (claim is not None and request.output_epoch != claim.output_epoch)
        ):
            raise VoiceRealtimeSessionError("playback receipt identity does not match the active response")
        input_turn_id = self._response_input_ids.get(request.response_id)
        turn = self._voice_turns.get(input_turn_id or "")
        expected_turn_id = str(turn["turn_id"]) if turn is not None else None
        actual_turn_id = getattr(request, "turn_id", None)
        actual_turn_id = str(actual_turn_id) if actual_turn_id is not None else None
        if actual_turn_id != expected_turn_id:
            raise VoiceRealtimeSessionError("playback receipt turn_id does not match the authorized response")
        payload = request.model_dump(mode="json")
        previous = state["receipts"].get(request.producer_seq)
        if previous is not None:
            if previous != payload:
                raise VoiceRealtimeSessionError("playback producer sequence conflicts with earlier facts")
            return False
        if request.producer_seq <= state["last_seq"]:
            raise VoiceRealtimeSessionError("playback producer sequence is not increasing")
        if state["terminal"]:
            raise VoiceRealtimeSessionError("playback response already has a terminal fact")
        if request.action == "open":
            if state["opened"] or request.start_ms is None:
                raise VoiceRealtimeSessionError("playback start is duplicate or incomplete")
            state["opened"] = True
        else:
            if not state["opened"]:
                raise VoiceRealtimeSessionError("playback lifecycle arrived before open")
            played_ms = request.played_ms
            if request.action == "audible":
                played_ms = request.audible_ms
            if played_ms is not None:
                if played_ms < state["played_ms"] or played_ms > state["scheduled_ms"]:
                    raise VoiceRealtimeSessionError("playback progress exceeds scheduled PCM or moves backwards")
                state["played_ms"] = played_ms
            if request.action in {"end", "interrupt"}:
                state["terminal"] = True
        state["last_seq"] = request.producer_seq
        state["receipts"][request.producer_seq] = payload
        return True

    async def process_utterance(
        self,
        source: CaptureRange,
        *,
        on_authorized: Any | None = None,
    ) -> InputCommitReceipt:
        """Commit one completed capture range and authorize only its bound final."""
        if not self.ready:
            raise VoiceRealtimeSessionError("realtime session is not ready")
        if source.session_id != self.session_id:
            raise VoiceRealtimeSessionError("capture range belongs to another chat")
        if source.media_session_id != self.media_session_id:
            raise VoiceRealtimeSessionError("capture range belongs to an old media session")

        async with self._input_lock:
            await self.emit_event(
                "voice_commit_sent",
                {
                    "capture_instance_id": str(source.capture_instance_id),
                    "start_frame": source.start_frame,
                    "end_frame": source.end_frame,
                },
            )
            await self.client.commit_audio()
            committed = await self.wait_for_input_committed()
            if not committed.item_id:
                raise VoiceRealtimeSessionError(
                    "cloud commit did not provide an input item id"
                )
            final = await self.wait_for_final(committed.item_id)
            if final.item_id != committed.item_id or final.is_final is not True:
                raise VoiceRealtimeSessionError(
                    "cloud final does not match the committed input item"
                )
            if not isinstance(final.transcript, str) or not final.transcript.strip():
                await self.emit_event(
                    "speech_without_usable_transcript",
                    {
                        "capture_instance_id": str(source.capture_instance_id),
                        "item_id": committed.item_id,
                    },
                )
                raise VoiceTranscriptUnavailable(
                    "committed speech has no usable final transcript"
                )

            identity = InputIdentity(
                session_id=self.session_id,
                input_turn_id=uuid4(),
                input_order=self._input_order,
                stimulus_revision=0,
            )
            self._input_order += 1
            value = FinalTranscript(
                identity=identity,
                capture_instance_id=source.capture_instance_id,
                media_session_id=self.media_session_id,
                item_id=committed.item_id,
                transcript=final.transcript,
                transcript_version=1,
                is_final=True,
            )
            await self.emit_event(
                "voice_transcript_final",
                {
                    "input_turn_id": str(identity.input_turn_id),
                    "item_id": value.item_id,
                    "transcript": value.transcript,
                    "transcript_version": value.transcript_version,
                    "is_final": True,
                },
                input_turn_id=str(identity.input_turn_id),
                stimulus_revision=identity.stimulus_revision,
            )
            receipt = await self._input_authorization.commit(value, source)
            await self.emit_event(
                "voice_input_committed",
                {
                    "capture_instance_id": str(source.capture_instance_id),
                    "deduplicated": receipt.deduplicated,
                    "identity": receipt.identity.model_dump(mode="json"),
                },
                input_turn_id=str(receipt.identity.input_turn_id),
                stimulus_revision=receipt.identity.stimulus_revision,
            )
            callback = on_authorized or self._on_input_authorized
            if not receipt.deduplicated and callback is not None:
                result = callback(receipt)
                if asyncio.iscoroutine(result):
                    await result
            return receipt

    async def emit_event(
        self,
        event_type: str,
        payload: dict[str, Any],
        *,
        input_turn_id: str | None = None,
        stimulus_revision: int | None = None,
    ) -> None:
        if self._event_sink is None:
            return
        result = self._event_sink(
            event_type,
            payload,
            self._event_seq,
            input_turn_id,
            stimulus_revision,
        )
        self._event_seq += 1
        if asyncio.iscoroutine(result):
            await result

    async def wait_for_final(self, item_id: str) -> ProtocolEvent:
        if not isinstance(item_id, str) or not item_id.strip():
            raise ValueError("item_id must be a non-empty string")
        if item_id in self._final_events:
            return self._final_events[item_id]
        if item_id not in self._final_futures:
            self._final_futures[item_id] = asyncio.get_running_loop().create_future()
        return await self._await_or_closed(
            asyncio.shield(self._final_futures[item_id])
        )

    async def run_receiver(self) -> None:
        if not self._ready:
            raise VoiceRealtimeSessionError("realtime session is not ready")
        async for event in self.client.events():
            self._route(event)

    def register_background_task(self, task: asyncio.Task[Any]) -> None:
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def close(self, reason: str = "manual") -> None:
        del reason
        if self._closed:
            return
        self._closed = True
        self._ready = False
        self._closed_event.set()
        await self.client.close()

    def _route(self, event: ProtocolEvent) -> None:
        if event.type == "input_audio_buffer_committed":
            if self._committed_events.full():
                raise VoiceRealtimeSessionError("input commit queue is overloaded")
            self._committed_events.put_nowait(event)

        if event.type == "input_transcript_final" and event.item_id:
            self._final_events[event.item_id] = event
            future = self._final_futures.get(event.item_id)
            if future is not None and not future.done():
                future.set_result(event)

        if event.response_id:
            local_response_id = self._supplier_response_map.get(event.response_id)
            if event.type == "response.created":
                if event.response_id in self._response_queues:
                    local_response_id = event.response_id
                elif local_response_id is None:
                    pending = [
                        response_id
                        for response_id in self._response_queues
                        if response_id not in self._supplier_response_map.values()
                    ]
                    if len(pending) == 1:
                        local_response_id = pending[0]
                if local_response_id is not None:
                    prior = self._supplier_response_map.get(event.response_id)
                    if prior is not None and prior != local_response_id:
                        raise VoiceRealtimeSessionError(
                            "supplier response id was rebound to another local response"
                        )
                    self._supplier_response_map[event.response_id] = local_response_id
            if local_response_id is None:
                local_response_id = event.response_id
            queue = self._response_queues.get(local_response_id)
            if queue is not None:
                if queue.full():
                    raise VoiceRealtimeSessionError(
                        f"response {local_response_id} queue is overloaded"
                    )
                queue.put_nowait(event)
        elif event.type == "response_failed":
            active = [queue for queue in self._response_queues.values()]
            if len(active) == 1:
                if active[0].full():
                    raise VoiceRealtimeSessionError("response queue is overloaded")
                active[0].put_nowait(event)


class VoiceRealtimeSessionRegistry:
    """Enforce one active media session per chat in this server process."""

    def __init__(self) -> None:
        self._sessions: dict[UUID, VoiceRealtimeSession] = {}
        self._by_media_session_id: dict[str, VoiceRealtimeSession] = {}
        self._recent_playback: dict[str, tuple[float, UUID, VoiceRealtimeSession]] = {}
        self._recent_playback_ttl_seconds = 1800
        self._recent_playback_limit = 64

    def register(
        self, session_id: UUID, session: VoiceRealtimeSession
    ) -> VoiceRealtimeSession:
        existing = self._sessions.get(session_id)
        if existing is not None:
            raise ActiveVoiceSessionExists(
                f"chat {session_id} already has active realtime session "
                f"{existing.media_session_id}"
            )
        self._sessions[session_id] = session
        self._by_media_session_id[session.media_session_id] = session
        return session

    def remove(
        self, session_id: UUID, session: VoiceRealtimeSession
    ) -> VoiceRealtimeSession:
        existing = self._sessions.get(session_id)
        if existing is not session:
            raise VoiceRealtimeSessionError(
                f"chat {session_id} does not own realtime session {session.media_session_id}"
            )
        del self._sessions[session_id]
        del self._by_media_session_id[session.media_session_id]
        self._prune_recent_playback()
        self._recent_playback[session.media_session_id] = (
            time.monotonic(), session_id, session
        )
        while len(self._recent_playback) > self._recent_playback_limit:
            oldest = min(
                self._recent_playback,
                key=lambda media_id: self._recent_playback[media_id][0],
            )
            del self._recent_playback[oldest]
        return session

    def get(self, session_id: UUID) -> VoiceRealtimeSession | None:
        return self._sessions.get(session_id)

    def get_by_media_session_id(self, media_session_id: str) -> VoiceRealtimeSession | None:
        return self._by_media_session_id.get(media_session_id)

    def get_playback_session(
        self, session_id: UUID, media_session_id: str
    ) -> VoiceRealtimeSession | None:
        active = self._sessions.get(session_id)
        if active is not None and active.media_session_id == media_session_id:
            return active
        self._prune_recent_playback()
        recent = self._recent_playback.get(media_session_id)
        if recent is None or recent[1] != session_id:
            return None
        return recent[2]

    def _prune_recent_playback(self) -> None:
        cutoff = time.monotonic() - self._recent_playback_ttl_seconds
        for media_id, (created_at, _session_id, _session) in list(
            self._recent_playback.items()
        ):
            if created_at < cutoff:
                del self._recent_playback[media_id]


def new_media_session_id() -> str:
    return f"media-{uuid4()}"


__all__ = [
    "ActiveVoiceSessionExists",
    "VoiceRealtimeSession",
    "VoiceRealtimeSessionError",
    "VoiceRealtimeSessionRegistry",
    "new_media_session_id",
]
