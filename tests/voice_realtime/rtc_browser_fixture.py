"""浏览器验收专用宿主：真实 RTC router，受控上下文与本地 SDP 对端。"""

import os
import hashlib
from uuid import UUID, uuid4

import httpx
from fastapi import FastAPI

from services.voice_rtc import RtcConfig, RtcContext
from services.voice_rtc_api import RtcRuntime
import services.voice_rtc_api as rtc_api


class FixturePool:
    def __init__(self):
        self.sessions = {}
        self.inputs = []
        self.outputs = []
        self.transcripts = []
        self.emotions = []
        self.bootstraps = 0
        self.offers = 0
        self.tool_calls = 0
        self.signaling_destinations = []

    def acquire(self):
        return self

    def transaction(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def fetchval(self, query, *args):
        if "save_rtc_user_transcript" in query:
            saved = {
                "message_id": str(uuid4()),
                "session_id": str(args[0]),
                "turn_id": str(args[3]),
                "input_turn_id": str(args[2]),
                "text": args[4],
            }
            self.transcripts.append(saved)
            return saved
        if "get_config_bool" in query:
            return True
        if "get_agent_consent_status" in query:
            return "consent"
        if "SELECT status" in query:
            return "active"
        if "INSERT INTO voice_realtime_sessions" in query:
            self.sessions[str(args[0])] = {
                "session_id": str(args[1]),
                "status": "active",
            }
            return args[0]
        if "start_rtc_voice_input" in query:
            turn = uuid4()
            self.inputs.append(
                {
                    "session_id": str(args[0]),
                    "media_session_id": str(args[1]),
                    "input_turn_id": str(args[2]),
                    "input_order": args[3],
                    "item_id": args[4],
                    "turn_id": str(turn),
                }
            )
            return turn
        if "record_rtc_voice_output" in query:
            self.outputs.append(
                {
                    "session_id": str(args[0]),
                    "turn_id": str(args[3]),
                    "input_turn_id": str(args[2]),
                    "response_id": args[4],
                    "text": args[5],
                    "generation_status": args[6],
                    "playback_scope": "unknown",
                }
            )
            return {"replayed": False}
        if "record_api_usage" in query:
            return None
        if "jsonb_build_object('status'" in query:
            return self.sessions.get(str(args[0]))
        raise AssertionError(query)

    async def execute(self, query, *args):
        if "closed_reason" in query and str(args[0]) in self.sessions:
            self.sessions[str(args[0])]["status"] = args[1]
        return "UPDATE 1"


pool = FixturePool()


async def context(_pool, *, session_id):
    pool.bootstraps += 1
    instructions = "测试对端上下文，仅用于本地自动化。"
    return RtcContext(instructions, hashlib.sha256(instructions.encode()).hexdigest())


async def exchange(_config, _key, offer):
    pool.offers += 1
    # 唯一网络目的地来自测试进程的本地 gateway，不接触真实供应商。
    gateway = int(os.environ["HEXIS_RTC_GATEWAY_PORT"])
    destination = f"http://127.0.0.1:{gateway}/offer"
    pool.signaling_destinations.append(destination)
    async with httpx.AsyncClient(trust_env=False, timeout=30) as client:
        response = await client.post(destination, content=offer)
    response.raise_for_status()
    return response.text


async def tool(session, call):
    session.validate_voice_tool_call(call)
    pool.tool_calls += 1
    return '{"status":"no_results","memories":[],"source":"controlled-fixture"}'


rtc_api.prepare_rtc_context = context
os.environ["HEXIS_RTC_FIXTURE_KEY"] = "controlled-fixture-credential"
config = RtcConfig.from_mapping(
    {
        "endpoint": "wss://workspace.fixture.example/api-ws/v1/realtime",
        "webrtc_endpoint": "https://workspace.fixture.example/api/v1/webrtc/realtime",
        "model": "controlled-fixture-model",
        "voice": "controlled-fixture-voice",
        "api_key_env": "HEXIS_RTC_FIXTURE_KEY",
        "timeout": 30,
    }
)
port = int(os.environ["HEXIS_RTC_FIXTURE_PORT"])


async def emotion(text):
    pool.emotions.append({"text": text, "label": "happy"})
    return {"label": "happy", "wordset_version": "v1"}


runtime = RtcRuntime(
    pool_provider=lambda: pool,
    config_loader=lambda: config,
    control_url=lambda media: f"ws://127.0.0.1:{port}/api/voice/realtime/sessions/{media}/control",
    exchange_sdp=exchange,
    tool_executor=tool,
    emotion_evaluator=emotion,
    tools_provider=lambda _surface: [
        {
            "type": "function",
            "function": {
                "name": "voice_recall",
                "description": "本地受控测试工具",
                "parameters": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                },
            },
        }
    ],
)
app = FastAPI()
app.include_router(runtime.router)


@app.get("/health")
async def health():
    return {"ok": True}


@app.get("/diagnostics")
async def diagnostics():
    return {
        "bootstraps": pool.bootstraps,
        "offers": pool.offers,
        "tool_calls": pool.tool_calls,
        "inputs": pool.inputs,
        "outputs": pool.outputs,
        "transcripts": pool.transcripts,
        "emotions": pool.emotions,
        "sessions": pool.sessions,
        "active_sessions": len(runtime.entries),
        "signaling_destinations": pool.signaling_destinations,
        "real_provider_requests": sum(
            not url.startswith("http://127.0.0.1:")
            for url in pool.signaling_destinations
        ),
    }
