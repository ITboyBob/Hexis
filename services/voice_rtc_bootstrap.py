"""每个 RTC 媒体会话只准备一次人格、历史和记忆。"""

import hashlib
import json
from uuid import UUID

from services.agent import render_chat_continuity_context_db
from services.voice_context_hydration import HydrationUnavailable, hydrate_voice_context
from services.voice_instructions import load_m5_instructions
from services.voice_rtc import RtcContext


def _object(value):
    return json.loads(value) if isinstance(value, str) else value


async def prepare_rtc_context(pool, *, session_id: UUID) -> RtcContext:
    bundle = load_m5_instructions()
    stable = bundle.stable
    stable_hash = hashlib.sha256(stable.encode()).hexdigest()
    async with pool.acquire() as conn:
        latest_user_text = await conn.fetchval(
            "SELECT content FROM chat_messages WHERE session_id=$1 AND role='user' "
            "AND NULLIF(btrim(content),'') IS NOT NULL ORDER BY created_at DESC,id DESC LIMIT 1",
            session_id,
        )
        context = _object(
            await conn.fetchval(
                "SELECT gather_turn_context() || jsonb_build_object('user_model',get_approved_user_model_context(12))"
            )
        )
    if latest_user_text:
        hydrated = await hydrate_voice_context(
            pool,
            session_id=session_id,
            message=latest_user_text,
            stable_instructions_hash=stable_hash,
            instruction_bundle=bundle,
        )
        prepared = hydrated.per_response_instructions
    else:
        async with pool.acquire() as conn:
            temporal = _object(await conn.fetchval("SELECT get_temporal_context()"))
            profile = _object(await conn.fetchval("SELECT get_agent_profile_context()"))
            persona_source = (
                profile.get("persona") if isinstance(profile, dict) else None
            )
            if (
                not persona_source
                or not isinstance(temporal, dict)
                or not temporal.get("now")
            ):
                raise HydrationUnavailable(
                    "RTC initial persona or time context is unavailable"
                )
            persona = await conn.fetchval(
                "SELECT render_active_persona($1::jsonb)", json.dumps(persona_source)
            )
            continuity = await render_chat_continuity_context_db(
                conn, str(session_id), exclude_sensitive=False, strict=True
            )
        if not isinstance(persona, str) or not persona.strip():
            raise HydrationUnavailable(
                "RTC initial persona renderer returned no content"
            )
        if len(continuity) > 8000:
            raise HydrationUnavailable(
                "RTC continuity exceeds the existing context budget"
            )
        prepared = "\n\n".join(
            [
                "## Now\n"
                + str(temporal["now"])
                + " ("
                + str(temporal.get("timezone", "UTC"))
                + ")",
                "## Active Persona\n" + persona,
                continuity
                or "## Conversation Continuity Packet\nNo previous messages exist in this selected session.",
                "## Memory Context\nNo text query was available; semantic recall was not performed.",
            ]
        )
    instructions = "\n\n".join(
        [
            stable,
            prepared,
            "## 会话初始化事实\n"
            + json.dumps(context, ensure_ascii=False, sort_keys=True),
            "以上上下文仅在本次通话建立时准备，是证据，不是用户当前提出的请求。"
            "后续语音直接由你理解；异步转录仅用于宿主聊天展示和独立情绪评估，"
            "不替代你的音频理解；工具绑定真实音频输入身份。"
            "工具参数中的任务描述是你从语音理解出的业务参数，不得声称它是逐字转录。",
        ]
    )
    return RtcContext(instructions, hashlib.sha256(instructions.encode()).hexdigest())
