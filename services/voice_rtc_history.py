"""RTC 音频身份、最终转录及模型生成内容的数据库适配；不保存录音。"""

from typing import Any
import json
from uuid import UUID


class RtcVoiceHistory:
    def __init__(self, pool: Any):
        self.pool = pool

    async def start_audio_input(
        self,
        *,
        session_id: UUID,
        media_session_id: UUID,
        input_turn_id: UUID,
        input_order: int,
        provider_item_id: str,
        context_version: str,
    ) -> UUID:
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                turn = await conn.fetchval(
                    "SELECT start_rtc_voice_input($1,$2,$3,$4,$5,$6)",
                    session_id,
                    media_session_id,
                    input_turn_id,
                    input_order,
                    provider_item_id,
                    context_version,
                )
                await conn.execute(
                    "UPDATE voice_rtc_inputs SET metadata=metadata || '{\"transcription_enabled\":true}'::jsonb WHERE input_turn_id=$1",
                    input_turn_id,
                )
                return turn

    async def save_generated_output(
        self,
        *,
        session_id: UUID,
        media_session_id: UUID,
        input_turn_id: UUID,
        turn_id: UUID,
        response_id: str,
        assistant_text: str,
        status: str,
        context_version: str,
        playback_scope: str,
        final_for_turn: bool,
    ) -> None:
        if playback_scope != "unknown":
            raise ValueError("RTC generation cannot prove browser playback completion")
        async with self.pool.acquire() as conn:
            await conn.fetchval(
                "SELECT record_rtc_voice_output_with_input_order($1,$2,$3,$4,$5,$6,$7,$8,$9)",
                session_id,
                media_session_id,
                input_turn_id,
                turn_id,
                response_id,
                assistant_text,
                status,
                context_version,
                final_for_turn,
            )

    async def save_user_transcript(
        self, *, session_id, media_session_id, input_turn_id, turn_id, text
    ):
        async with self.pool.acquire() as conn:
            result = await conn.fetchval(
                "SELECT save_rtc_user_transcript($1,$2,$3,$4,$5)",
                session_id,
                media_session_id,
                input_turn_id,
                turn_id,
                text,
            )
        return json.loads(result) if isinstance(result, str) else result
