"""Read-only, allowlisted memory recall for authorized realtime voice turns."""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from core.cognitive_memory_api import CognitiveMemory

VOICE_RECALL_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "voice_recall",
        "description": "Search Hexis memory for relevant information to answer the current authorized voice input.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "A concise query for relevant remembered facts.",
                }
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
}


class VoiceRecallError(ValueError):
    """The provider attempted a malformed or unsupported voice tool call."""


class VoiceRecallService:
    def __init__(self, pool: Any, *, max_results: int = 5) -> None:
        if pool is None:
            raise ValueError("a database pool is required for voice recall")
        if isinstance(max_results, bool) or not isinstance(max_results, int) or not 1 <= max_results <= 10:
            raise ValueError("max_results must be between 1 and 10")
        self._pool = pool
        self._max_results = max_results

    async def execute(
        self,
        *,
        session_id: UUID,
        input_turn_id: UUID,
        name: str,
        arguments: str,
    ) -> str:
        if name != "voice_recall":
            raise VoiceRecallError("unsupported tool name")
        if not isinstance(session_id, UUID) or not isinstance(input_turn_id, UUID):
            raise VoiceRecallError("voice recall requires an authorized input identity")
        try:
            value = json.loads(arguments)
        except (json.JSONDecodeError, TypeError) as exc:
            raise VoiceRecallError("voice recall arguments must be valid JSON") from exc
        if not isinstance(value, dict) or set(value) != {"query"}:
            raise VoiceRecallError("voice recall accepts only the required query field")
        query = value["query"]
        if not isinstance(query, str) or not query.strip() or len(query) > 2000:
            raise VoiceRecallError("voice recall query must contain 1 to 2000 characters")

        result = await CognitiveMemory(self._pool).recall(
            query.strip(),
            limit=self._max_results,
            include_partial=True,
        )
        memories = [
            {
                "id": str(memory.id),
                "type": memory.type.value,
                "content": memory.content[:2000],
                "importance": memory.importance,
                "trust_level": memory.trust_level,
                "source_attribution": memory.source_attribution,
            }
            for memory in result.memories[: self._max_results]
        ]
        partial = [
            {
                "cluster_name": activation.cluster_name,
                "keywords": activation.keywords[:20],
                "similarity": activation.cluster_similarity,
            }
            for activation in result.partial_activations[: self._max_results]
        ]
        payload = {
            "status": "found" if memories or partial else "no_results",
            "query": query.strip(),
            "memories": memories,
            "partial_activations": partial,
        }
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
