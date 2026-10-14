"""仅使用替身验证 RTC 情绪配置、上下文和评估连接，不调用真实模型。"""

import json
from unittest.mock import AsyncMock

import pytest

import services.voice_rtc_emotion as emotion

pytestmark = pytest.mark.asyncio(loop_scope="function")


def config_file(tmp_path, monkeypatch, timeout=3000):
    path = tmp_path / "emotion.json"
    path.write_text(
        json.dumps(
            {
                "emotion": {
                    "provider": "aliyun-systemone",
                    "model": "decision-model-preview",
                    "endpoint": "https://fixture.invalid/systemone",
                    "api_key_env": "RTC_EMOTION_TEST_KEY",
                    "timeout_ms": timeout,
                }
            }
        )
    )
    monkeypatch.setenv("HEXIS_M4_VOICE_CONFIG", str(path))
    monkeypatch.setenv("RTC_EMOTION_TEST_KEY", "fixture-only")


async def test_existing_evaluator_receives_transcript_and_four_contexts(
    tmp_path, monkeypatch
):
    config_file(tmp_path, monkeypatch)
    context = {key: [] for key in ("identity", "worldview", "goals", "relationships")}
    conn = object()

    class Pool:
        def acquire(self):
            return self

        async def __aenter__(self):
            return conn

        async def __aexit__(self, *args):
            return False

    read = AsyncMock(return_value=context)
    evaluate = AsyncMock(return_value={"label": "curious", "wordset_version": "v1"})
    monkeypatch.setattr(emotion, "read_db_context", read)
    monkeypatch.setattr(emotion, "evaluate", evaluate)
    result = await emotion.make_rtc_emotion_evaluator(lambda: Pool())("你好")
    assert result["label"] == "curious"
    read.assert_awaited_once_with(conn)
    assert evaluate.await_args.args[:2] == ("你好", context)


@pytest.mark.parametrize("timeout", [0, -1, True, float("inf")])
async def test_invalid_timeout_is_rejected(tmp_path, monkeypatch, timeout):
    config_file(tmp_path, monkeypatch, timeout)
    with pytest.raises(ValueError):
        emotion.load_emotion_config()


async def test_missing_key_fails_only_when_branch_runs(tmp_path, monkeypatch):
    config_file(tmp_path, monkeypatch)
    monkeypatch.delenv("RTC_EMOTION_TEST_KEY")
    run = emotion.make_rtc_emotion_evaluator(lambda: None)
    with pytest.raises(ValueError):
        await run("你好")
