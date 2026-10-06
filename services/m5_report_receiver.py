"""Transactional M5 report delivery into the bound source chat."""

from __future__ import annotations

import json
from typing import Any, Callable
from uuid import UUID


class ReportDeliveryError(RuntimeError):
    """The report could not be accepted by its authoritative source chat."""


class M5ReportReceiver:
    def __init__(self, pool: Any, *, on_next_work: Callable[[], None] | None = None):
        self._pool = pool
        self._on_next_work = on_next_work

    def set_next_work_callback(self, callback: Callable[[], None] | None) -> None:
        self._on_next_work = callback

    async def deliver(
        self,
        report_id: UUID | str,
        bound_origin: UUID | str,
        summary: str,
        actual_call_id: str,
    ) -> dict[str, Any]:
        try:
            report_uuid = UUID(str(report_id))
            origin_uuid = UUID(str(bound_origin))
        except (TypeError, ValueError) as exc:
            raise ReportDeliveryError("report or origin identity is invalid") from exc
        if not isinstance(summary, str) or not summary.strip():
            raise ReportDeliveryError("report summary must not be blank")
        if not isinstance(actual_call_id, str) or not actual_call_id.strip():
            raise ReportDeliveryError("report call id must be preserved from the model")

        async with self._pool.acquire() as conn:
            try:
                async with conn.transaction():
                    raw = await conn.fetchval(
                        "SELECT public.deliver_m5_report($1,$2,$3,$4)",
                        report_uuid,
                        origin_uuid,
                        summary,
                        actual_call_id,
                    )
            except Exception as exc:
                message = str(exc).lower()
                reason = "origin" if "origin" in message else "report delivery was rejected"
                raise ReportDeliveryError(reason) from exc

        receipt = json.loads(raw) if isinstance(raw, str) else raw
        if not isinstance(receipt, dict) or receipt.get("outcome") != "delivered":
            raise ReportDeliveryError("report receiver returned no accepted delivery receipt")
        if self._on_next_work and (receipt.get("round") or {}).get("next_work_execution_id"):
            self._on_next_work()
        return receipt
