"""Transactional M5 report delivery into the bound source chat."""

from __future__ import annotations

import json
from typing import Any, Callable
from uuid import UUID


class ReportDeliveryError(RuntimeError):
    """The report could not be accepted by its authoritative source chat."""

    def __init__(
        self, code: str, message: str | None = None, *, retryable: bool = False
    ) -> None:
        self.code = code
        self.retryable = retryable
        super().__init__(message or code)


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
            raise ReportDeliveryError("INVALID_REPORT_IDENTITY") from exc
        if not isinstance(summary, str) or not summary.strip():
            raise ReportDeliveryError("INVALID_SUMMARY")
        if not isinstance(actual_call_id, str) or not actual_call_id.strip():
            raise ReportDeliveryError("MISSING_CALL_ID")

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
                if "origin does not match" in message or "task binding" in message:
                    code = "TARGET_OWNERSHIP_MISMATCH"
                    retryable = False
                elif "origin session is unavailable" in message:
                    code = "ORIGIN_SESSION_UNAVAILABLE"
                    retryable = False
                elif "report identity is unavailable" in message:
                    code = "REPORT_NOT_FOUND"
                    retryable = False
                elif "work has not ended" in message:
                    code = "WORK_NOT_TERMINAL"
                    retryable = False
                else:
                    code = "DELIVERY_TEMPORARY_FAILURE"
                    retryable = True
                raise ReportDeliveryError(code, str(exc), retryable=retryable) from exc

        receipt = json.loads(raw) if isinstance(raw, str) else raw
        if not isinstance(receipt, dict) or receipt.get("outcome") != "delivered":
            raise ReportDeliveryError(
                "DELIVERY_TEMPORARY_FAILURE",
                "report receiver returned no accepted delivery receipt",
                retryable=True,
            )
        receiver_call_id = receipt.get("actual_call_id")
        if receiver_call_id != actual_call_id:
            receipt = receipt | {
                "receiver_actual_call_id": receiver_call_id,
                "actual_call_id": actual_call_id,
                "replayed": True,
            }
        else:
            receipt = receipt | {"replayed": False}
        if self._on_next_work and (receipt.get("round") or {}).get("next_work_execution_id"):
            self._on_next_work()
        return receipt
