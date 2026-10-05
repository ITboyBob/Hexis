"""Per-chat arbitration for one active native voice output."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from uuid import UUID


class OutputClaimConflict(RuntimeError):
    """Another response already owns the chat's foreground output."""


@dataclass(frozen=True)
class OutputClaim:
    response_id: str
    media_session_id: str
    output_epoch: int


class VoiceOutputArbiter:
    """Serialize output ownership without coupling it to background tasks."""

    def __init__(self, *, session_id: UUID):
        self.session_id = session_id
        self._lock = asyncio.Lock()
        self._claims: dict[str, OutputClaim] = {}
        self._active_response_id: str | None = None
        self._next_epoch = 1

    @property
    def active_response_id(self) -> str | None:
        return self._active_response_id

    async def claim(
        self, response_id: str, media_session_id: str
    ) -> OutputClaim:
        if not isinstance(response_id, str) or not response_id.strip():
            raise ValueError("response_id must be a non-empty string")
        if not isinstance(media_session_id, str) or not media_session_id.strip():
            raise ValueError("media_session_id must be a non-empty string")

        async with self._lock:
            if self._active_response_id is not None:
                active = self._claims[self._active_response_id]
                if active.response_id == response_id:
                    if active.media_session_id != media_session_id:
                        raise OutputClaimConflict(
                            "output media session mismatch: "
                            f"expected={active.media_session_id} actual={media_session_id}"
                        )
                    raise OutputClaimConflict(
                        f"response {response_id} already owns the foreground output"
                    )
                raise OutputClaimConflict(
                    "another response already owns the foreground output: "
                    f"{active.response_id}"
                )
            existing = self._claims.get(response_id)
            if existing is not None:
                if existing.media_session_id != media_session_id:
                    raise OutputClaimConflict(
                        "output media session mismatch: "
                        f"expected={existing.media_session_id} actual={media_session_id}"
                    )
                raise OutputClaimConflict(
                    f"response {response_id} already owns the foreground output"
                )
            claim = OutputClaim(
                response_id=response_id,
                media_session_id=media_session_id,
                output_epoch=self._next_epoch,
            )
            self._claims[response_id] = claim
            self._active_response_id = response_id
            self._next_epoch += 1
            return claim

    async def release(self, response_id: str, media_session_id: str) -> None:
        async with self._lock:
            existing = self._claims.get(response_id)
            if existing is None or existing.media_session_id != media_session_id:
                raise OutputClaimConflict(
                    f"cannot release unowned output {response_id} for {media_session_id}"
                )
            del self._claims[response_id]
            if self._active_response_id == response_id:
                self._active_response_id = None
