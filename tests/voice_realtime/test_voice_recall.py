import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

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
