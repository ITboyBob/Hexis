"""RTC 独立情绪支路：惰性读取配置，不参与语音回复闸门。"""

import asyncio
import json
import math
import os
import re
from pathlib import Path

from services.avatar_emotion import evaluate, read_db_context


def load_emotion_config():
    path = Path(
        os.environ.get("HEXIS_M4_VOICE_CONFIG")
        or Path(__file__).resolve().parents[1] / "config/m4-voice-runtime.json"
    )
    emotion = json.loads(path.read_text(encoding="utf-8"))["emotion"]
    for name in ("provider", "model", "endpoint", "api_key_env"):
        value = emotion[name]
        if not isinstance(value, str) or not value.strip() or value != value.strip():
            raise ValueError(f"emotion.{name} 必须是非空字符串")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", emotion["api_key_env"]):
        raise ValueError("情绪凭据必须使用环境变量名")
    timeout = emotion["timeout_ms"]
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not math.isfinite(timeout)
        or timeout <= 0
    ):
        raise ValueError("情绪超时必须是有限正数")
    key = os.environ.get(emotion["api_key_env"])
    if not key:
        raise ValueError("所选情绪模型凭据不可用")
    return {
        **{name: emotion[name] for name in ("provider", "model", "endpoint")},
        "resolved_api_key": key,
        "timeout_seconds": timeout / 1000,
    }


def make_rtc_emotion_evaluator(pool_provider):
    async def run(text):
        config = load_emotion_config()

        async def appraisal():
            async with pool_provider().acquire() as conn:
                context = await read_db_context(conn)
            return await evaluate(text, context, config)

        return await asyncio.wait_for(appraisal(), config["timeout_seconds"])

    return run
