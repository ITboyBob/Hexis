"""Loopback-only API for explicitly selected Avatar preview evaluations."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import re
import sys
from collections.abc import Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse
from uuid import UUID

import asyncpg
import uvicorn
from dotenv import dotenv_values
from fastapi import FastAPI, HTTPException, Path as ApiPath
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from services import avatar_emotion

logger = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parents[1]
ENV_NAME = re.compile(r"[A-Z_][A-Z0-9_]*\Z")


class PreviewConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    endpoint: str = Field(min_length=1)
    api_key_env: str = Field(min_length=1)
    timeout_seconds: float = Field(gt=0, allow_inf_nan=False)
    total_timeout_seconds: float = Field(gt=0, allow_inf_nan=False)
    fixture: dict[str, Any]
    db_dsn_env: str | None = None

    @field_validator("api_key_env", "db_dsn_env")
    @classmethod
    def validate_environment_name(cls, value: str | None) -> str | None:
        if value is not None and not ENV_NAME.fullmatch(value):
            raise ValueError("配置项必须是精确的环境变量名")
        return value

    @field_validator("endpoint")
    @classmethod
    def validate_endpoint(cls, value: str) -> str:
        parsed = urlparse(value)
        if parsed.scheme not in {"https", "http"} or not parsed.netloc:
            raise ValueError("endpoint 必须是完整的 HTTP(S) URL")
        return value

    @field_validator("fixture")
    @classmethod
    def validate_fixture(cls, value: dict[str, Any]) -> dict[str, Any]:
        avatar_emotion.select_context(value)
        return value


class IdentityDTO(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    session_id: UUID
    input_turn_id: UUID
    input_order: int = Field(ge=0)
    stimulus_revision: int = Field(ge=0)


class EvaluationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    identity: IdentityDTO
    message: str = Field(min_length=1)
    context_source: Literal["fixture", "db"]

    @field_validator("message")
    @classmethod
    def reject_blank_message(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("消息不能为空")
        return value


def load_config(path: str | Path) -> PreviewConfig:
    """Load an explicit, non-secret JSON profile from the path supplied by the user."""
    source = Path(path)
    try:
        value = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(
            "无法读取 Avatar 预览配置；请检查明确选择的 --config 文件"
        ) from exc
    if not isinstance(value, dict):
        raise ValueError("Avatar 预览配置必须是 JSON 对象")
    return PreviewConfig.model_validate(value)


def _selected_environment(
    config: PreviewConfig,
    process_environment: Mapping[str, str],
    env_file: Path,
) -> dict[str, str]:
    """Resolve only the explicitly named variables, preferring the selected .env file."""
    file_values = dotenv_values(env_file)
    names = [config.api_key_env]
    if config.db_dsn_env:
        names.append(config.db_dsn_env)
    selected: dict[str, str] = {}
    for name in names:
        file_value = file_values.get(name)
        process_value = process_environment.get(name)
        value = file_value if file_value is not None else process_value
        if value is not None:
            selected[name] = value
    return selected


async def _connect(dsn: str, *, timeout: float) -> asyncpg.Connection:
    return await asyncpg.connect(dsn, timeout=timeout)


def _consume_task_result(task: asyncio.Task[Any]) -> None:
    if task.cancelled():
        return
    error = task.exception()
    if error is not None:
        logger.warning(
            "Avatar preview background cleanup failed error_type=%s",
            type(error).__name__,
        )


def _detach_task(
    task: asyncio.Future[Any],
    *,
    on_result: Callable[[Any], None] | None = None,
    on_error: Callable[[BaseException], None] | None = None,
) -> None:
    task.cancel()

    def consume(done: asyncio.Future[Any]) -> None:
        if done.cancelled():
            return
        try:
            value = done.result()
        except asyncio.CancelledError:
            return
        except Exception as exc:
            if on_error is not None:
                try:
                    on_error(exc)
                except Exception as callback_error:
                    logger.warning(
                        "Avatar preview detached error handling failed error_type=%s",
                        type(callback_error).__name__,
                    )
            else:
                logger.warning(
                    "Avatar preview detached operation failed error_type=%s",
                    type(exc).__name__,
                )
            return
        if on_result is not None:
            try:
                on_result(value)
            except Exception as exc:
                logger.warning(
                    "Avatar preview detached result handling failed error_type=%s",
                    type(exc).__name__,
                )

    task.add_done_callback(consume)


def _late_connection_cleanup(connection: Any) -> None:
    if _terminate_connection(connection):
        return
    close = getattr(connection, "close", None)
    if close is None:
        logger.warning("Avatar preview late connection has no cleanup method")
        return
    try:
        task = asyncio.create_task(close())
        task.add_done_callback(_consume_task_result)
    except Exception as exc:
        logger.warning(
            "Avatar preview late connection close could not start error_type=%s",
            type(exc).__name__,
        )


def _terminate_connection(connection: Any) -> bool:
    terminate = getattr(connection, "terminate", None)
    if terminate is None:
        return False
    try:
        terminate()
        return True
    except Exception as exc:
        logger.warning(
            "Avatar preview connection termination failed error_type=%s",
            type(exc).__name__,
        )
        return False


def _deadline_reason(
    now: float, stage_deadline: float, total_deadline: float
) -> str | None:
    limiting_deadline = min(stage_deadline, total_deadline)
    if now < limiting_deadline:
        return None
    return "total_deadline" if total_deadline <= stage_deadline else "stage_timeout"


async def _await_until_deadline(
    awaitable: Awaitable[Any],
    *,
    stage: str,
    stage_deadline: float,
    total_deadline: float,
    cleanup_result: Callable[[Any], None] | None = None,
    on_deadline_result: Callable[[Any], None] | None = None,
    on_deadline_error: Callable[[BaseException], None] | None = None,
) -> Any:
    loop = asyncio.get_running_loop()
    task = asyncio.ensure_future(awaitable)

    def timeout_failure(reason: str) -> EvaluationTimeout:
        if stage == "model" and reason == "stage_timeout":
            reason = "model_timeout"
        closes_window = reason in {"model_timeout", "total_deadline"}
        return EvaluationTimeout(
            stage,
            reason,
            closes_window=closes_window,
        )

    while True:
        reason = _deadline_reason(loop.time(), stage_deadline, total_deadline)
        if reason is not None:
            _detach_task(
                task,
                on_result=on_deadline_result or cleanup_result,
                on_error=on_deadline_error,
            )
            raise timeout_failure(reason)
        timeout = min(stage_deadline, total_deadline) - loop.time()
        try:
            done, _pending = await asyncio.wait({task}, timeout=max(0, timeout))
        except asyncio.CancelledError:
            _detach_task(task, on_result=cleanup_result)
            raise
        reason = _deadline_reason(loop.time(), stage_deadline, total_deadline)
        if reason is not None:
            _detach_task(
                task,
                on_result=on_deadline_result or cleanup_result,
                on_error=on_deadline_error,
            )
            raise timeout_failure(reason)
        if done:
            try:
                return task.result()
            except asyncio.TimeoutError as exc:
                if stage.startswith("context_"):
                    raise EvaluationTimeout(
                        stage, "context_stage_timeout", closes_window=False
                    ) from exc
                raise


def _identity_json(value: IdentityDTO | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(value, IdentityDTO):
        return value.model_dump(mode="json")
    return {
        "session_id": str(value["session_id"]),
        "input_turn_id": str(value["input_turn_id"]),
        "input_order": int(value["input_order"]),
        "stimulus_revision": int(value["stimulus_revision"]),
    }


def evaluation_path(identity: IdentityDTO | Mapping[str, Any]) -> str:
    value = _identity_json(identity)
    return (
        f"/evaluations/{value['session_id']}/{value['input_turn_id']}"
        f"/{value['stimulus_revision']}"
    )


class Evaluations:
    """Single-process identity registry; shielded work survives a lost HTTP waiter."""

    def __init__(self) -> None:
        self.entries: dict[
            tuple[str, str, int],
            tuple[str, asyncio.Task[dict[str, Any]], dict[str, Any]],
        ] = {}

    @staticmethod
    def _canonical_body(body: Mapping[str, Any]) -> dict[str, Any]:
        value = dict(body)
        value["identity"] = _identity_json(value["identity"])
        return value

    async def get_or_start(
        self,
        body: Mapping[str, Any],
        operation: Callable[[], Awaitable[dict[str, Any]]],
    ) -> dict[str, Any]:
        canonical = self._canonical_body(body)
        identity = canonical["identity"]
        key = (
            identity["session_id"],
            identity["input_turn_id"],
            identity["stimulus_revision"],
        )
        fingerprint = hashlib.sha256(
            json.dumps(
                canonical, sort_keys=True, ensure_ascii=False, separators=(",", ":")
            ).encode()
        ).hexdigest()
        previous = self.entries.get(key)
        if previous:
            old_fingerprint, future, _metadata = previous
            if old_fingerprint != fingerprint:
                raise HTTPException(
                    status_code=409,
                    detail="同一输入身份的内容发生变化；请提交新 revision",
                )
            return await asyncio.shield(future)

        future = asyncio.create_task(operation())
        self.entries[key] = (
            fingerprint,
            future,
            {
                "identity": identity,
                "wordset_version": avatar_emotion.WORDSET_VERSION,
                "emotion_source": "real",
                "context_source": canonical["context_source"],
                "visual_owner": "service",
                "visual_window": _window_fact("open"),
            },
        )
        return await asyncio.shield(future)

    async def query(
        self, session_id: UUID, input_turn_id: UUID, revision: int
    ) -> dict[str, Any] | None:
        entry = self.entries.get((str(session_id), str(input_turn_id), revision))
        if entry is None:
            return None
        _fingerprint, future, metadata = entry
        if not future.done():
            return {
                "status": "pending",
                **metadata,
            }
        if future.cancelled():
            return {
                **metadata,
                "status": "error",
                "error_code": "ProcessShutdown",
                "message": "预览服务正在关闭；请重新打开会话后再提交。",
            }
        return future.result()

    async def close(self) -> None:
        pending = [
            future
            for _fingerprint, future, _metadata in self.entries.values()
            if not future.done()
        ]
        for future in pending:
            future.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)


class EvaluationFailure(Exception):
    def __init__(self, stage: str, cause: Exception) -> None:
        super().__init__(type(cause).__name__)
        self.stage = stage
        self.cause = cause


class EvaluationTimeout(Exception):
    def __init__(self, stage: str, reason: str, *, closes_window: bool) -> None:
        super().__init__(stage)
        self.stage = stage
        self.reason = reason
        self.closes_window = closes_window


def _window_fact(status: str, *, reason: str | None = None) -> dict[str, Any]:
    fact: dict[str, Any] = {
        "status": status,
        "owner": "service",
        "decision_seq": 0 if status == "open" else 1,
    }
    if reason is not None:
        fact["reason"] = reason
    return fact


def _error_result(
    identity: Mapping[str, Any],
    context_source: str,
    stage: str,
    error_code: str,
    *,
    logical_call_count: int = 0,
    visual_window: Mapping[str, Any] | None = None,
    transport_status: str | None = None,
    late_diagnostics: list[dict[str, Any]] | None = None,
    cleanup_diagnostics: list[dict[str, Any]] | None = None,
    raw_business_value: Any = None,
) -> dict[str, Any]:
    if error_code == "ConfigurationUnavailable":
        message = "所选模型凭据不可用；请检查配置中指定的环境变量。"
    elif error_code == "ContextSourceUnavailable":
        message = "所选上下文来源不可用；请选择已配置来源后用新输入重试。"
    elif error_code in {"ModelEvaluationTimeout", "TotalDeadlineExceeded"}:
        message = "本次评估超时，最近有效情绪保持不变；请检查服务与网络后用新输入重试。"
    elif error_code == "ContextStageTimeout":
        message = (
            "读取所选上下文超时；最近有效情绪保持不变，请检查上下文来源后用新输入重试。"
        )
    elif stage == "context" or stage.startswith("context_"):
        message = "所选上下文缺失或格式无效，最近有效情绪保持不变；请检查上下文来源后用新输入重试。"
    elif error_code == "ValueError" or error_code == "InvalidEmotionLabel":
        message = "模型结果不符合情绪标签协议，最近有效情绪保持不变；请检查诊断后用新输入重试。"
    else:
        message = (
            "本次评估失败，最近有效情绪保持不变；请检查所选配置与来源后用新输入重试。"
        )
    result = {
        "identity": dict(identity),
        "status": "error",
        "wordset_version": avatar_emotion.WORDSET_VERSION,
        "emotion_source": "real",
        "context_source": context_source,
        "error_code": error_code,
        "stage": stage,
        "logical_call_count": logical_call_count,
        "transport_attempts": None,
        "message": message,
    }
    if visual_window is not None:
        result["visual_window"] = dict(visual_window)
    if transport_status is not None:
        result["transport_status"] = transport_status
    if late_diagnostics is not None:
        result["late_diagnostics"] = late_diagnostics
    if cleanup_diagnostics is not None:
        result["cleanup_diagnostics"] = cleanup_diagnostics
    if error_code == "InvalidEmotionLabel":
        result["raw_business_value"] = raw_business_value
        result["rejection_reason"] = "not_exact_v1_emotion_label"
    return result


async def run_evaluation(
    body: Mapping[str, Any],
    config: PreviewConfig | None,
    *,
    api_key: str | None,
    resolved_db_dsn: str | None,
    conn_factory: Callable[..., Awaitable[Any]],
    evaluator: Callable[..., Awaitable[dict[str, Any]]],
) -> dict[str, Any]:
    canonical = Evaluations._canonical_body(body)
    identity = canonical["identity"]
    context_source = canonical["context_source"]
    stage = "configuration"
    model_started = False
    late_diagnostics: list[dict[str, Any]] = []
    cleanup_diagnostics: list[dict[str, Any]] = []
    loop = asyncio.get_running_loop()
    total_deadline = loop.time() + (config.total_timeout_seconds if config else 1)

    if config is None:
        return _error_result(
            identity, context_source, "configuration", "ConfigurationUnavailable"
        )

    async def perform() -> dict[str, Any]:
        nonlocal stage, model_started
        if not api_key or not api_key.strip():
            raise EvaluationFailure(
                "configuration",
                RuntimeError("Selected model credential is unavailable"),
            )

        stage = "context"
        if context_source == "fixture":
            context = avatar_emotion.select_context(config.fixture)
        else:
            if not config.db_dsn_env or not resolved_db_dsn:
                raise EvaluationFailure(
                    "context",
                    RuntimeError(
                        "Selected database DSN environment variable is unavailable"
                    ),
                )
            stage = "context_connect"
            connect_deadline = loop.time() + config.timeout_seconds
            connection = await _await_until_deadline(
                conn_factory(resolved_db_dsn, timeout=config.timeout_seconds),
                stage=stage,
                stage_deadline=connect_deadline,
                total_deadline=total_deadline,
                cleanup_result=_late_connection_cleanup,
            )
            cancelled_during_read = False
            try:

                async def read_context() -> dict[str, Any]:
                    async with connection.transaction(readonly=True):
                        return await avatar_emotion.read_db_context(connection)

                stage = "context_read"
                read_deadline = loop.time() + config.timeout_seconds
                context = await _await_until_deadline(
                    read_context(),
                    stage=stage,
                    stage_deadline=read_deadline,
                    total_deadline=total_deadline,
                )
            except asyncio.CancelledError:
                cancelled_during_read = True
                raise
            finally:
                original_exception = sys.exc_info()[1]
                original_stage = stage
                stage = "context_close"
                close_deadline = loop.time() + config.timeout_seconds
                close_task = asyncio.create_task(connection.close())
                try:
                    if cancelled_during_read:
                        await asyncio.sleep(0)
                        if not close_task.done():
                            close_task.cancel()
                            close_task.add_done_callback(_consume_task_result)
                            _terminate_connection(connection)
                        else:
                            close_task.result()
                    else:
                        if loop.time() >= total_deadline:
                            await asyncio.sleep(0)
                            if not close_task.done():
                                close_task.cancel()
                                close_task.add_done_callback(_consume_task_result)
                                _terminate_connection(connection)
                            else:
                                _consume_task_result(close_task)
                            failure_stage = (
                                original_exception.stage
                                if isinstance(original_exception, EvaluationTimeout)
                                and original_exception.reason == "total_deadline"
                                else stage
                            )
                            raise EvaluationTimeout(
                                failure_stage, "total_deadline", closes_window=True
                            )
                        await _await_until_deadline(
                            close_task,
                            stage=stage,
                            stage_deadline=close_deadline,
                            total_deadline=total_deadline,
                        )
                except BaseException as cleanup_error:
                    cleanup_diagnostics.append(
                        {
                            "stage": "context_close",
                            "error_type": type(cleanup_error).__name__,
                        }
                    )
                    logger.warning(
                        "Avatar preview DB cleanup failed stage=context_close error_type=%s",
                        type(cleanup_error).__name__,
                    )
                    _terminate_connection(connection)
                    if isinstance(cleanup_error, asyncio.CancelledError):
                        raise
                    if original_exception is not None and not (
                        isinstance(cleanup_error, EvaluationTimeout)
                        and cleanup_error.reason == "total_deadline"
                    ):
                        stage = original_stage
                        raise original_exception.with_traceback(
                            original_exception.__traceback__
                        )
                    raise
                if original_exception is not None:
                    stage = original_stage

        if loop.time() >= total_deadline:
            raise EvaluationTimeout(stage, "total_deadline", closes_window=True)
        stage = "model"
        model_started = True
        model_deadline = loop.time() + config.timeout_seconds
        llm_config = {
            "provider": config.provider,
            "model": config.model,
            "endpoint": config.endpoint,
            "resolved_api_key": api_key,
            "timeout_seconds": config.timeout_seconds,
            "host_managed_timeout": True,
        }

        def record_late_result(value: Any) -> None:
            if not isinstance(value, Mapping):
                return
            diagnostic: dict[str, Any] = {
                "identity": identity,
                "status": "valid" if isinstance(value.get("label"), str) else "error",
                "accepted": False,
            }
            if "label" in value:
                diagnostic["label"] = value["label"]
            if "raw_business_value" in value:
                diagnostic["raw_business_value"] = value["raw_business_value"]
            late_diagnostics.append(diagnostic)

        def record_late_error(error: BaseException) -> None:
            diagnostic: dict[str, Any] = {
                "identity": identity,
                "status": "error",
                "accepted": False,
                "error_type": type(error).__name__,
            }
            if not isinstance(error, avatar_emotion.InvalidEmotionLabel):
                late_diagnostics.append(diagnostic)
                logger.warning(
                    "Avatar preview detached model operation failed error_type=%s",
                    type(error).__name__,
                )
                return
            diagnostic["raw_business_value"] = error.raw_business_value
            diagnostic["rejection_reason"] = "not_exact_v1_emotion_label"
            late_diagnostics.append(diagnostic)

        result = await _await_until_deadline(
            evaluator(canonical["message"], context, llm_config),
            stage="model",
            stage_deadline=model_deadline,
            total_deadline=total_deadline,
            on_deadline_result=record_late_result,
            on_deadline_error=record_late_error,
        )
        expired = _deadline_reason(loop.time(), model_deadline, total_deadline)
        if expired is not None:
            raise EvaluationTimeout(
                "model",
                expired if expired == "total_deadline" else "model_timeout",
                closes_window=True,
            )
        return {
            "identity": identity,
            **result,
            "status": "valid",
            "emotion_source": "real",
            "context_source": context_source,
            "visual_window": _window_fact("accepted"),
        }

    try:
        return await perform()
    except asyncio.CancelledError:
        raise
    except EvaluationTimeout as failure:
        logger.warning(
            "Avatar preview evaluation failed stage=%s error_type=%s",
            failure.stage,
            type(failure).__name__,
        )
        code = (
            "TotalDeadlineExceeded"
            if failure.reason == "total_deadline"
            else (
                "ModelEvaluationTimeout"
                if failure.reason == "model_timeout"
                else "ContextStageTimeout"
            )
        )
        return _error_result(
            identity,
            context_source,
            failure.stage,
            code,
            logical_call_count=int(model_started),
            visual_window=(
                _window_fact("closed", reason=failure.reason)
                if failure.closes_window
                else None
            ),
            transport_status="unknown" if model_started else None,
            late_diagnostics=late_diagnostics if model_started else None,
            cleanup_diagnostics=cleanup_diagnostics or None,
        )
    except asyncio.TimeoutError:
        logger.warning(
            "Avatar preview evaluation failed stage=%s error_type=TimeoutError", stage
        )
        return _error_result(
            identity,
            context_source,
            stage,
            "TimeoutError",
            logical_call_count=int(model_started),
            transport_status="unknown" if model_started else None,
            late_diagnostics=late_diagnostics if model_started else None,
            cleanup_diagnostics=cleanup_diagnostics or None,
        )
    except EvaluationFailure as failure:
        logger.warning(
            "Avatar preview evaluation failed stage=%s error_type=%s",
            failure.stage,
            type(failure.cause).__name__,
        )
        code = (
            "ConfigurationUnavailable"
            if failure.stage == "configuration"
            else "ContextSourceUnavailable"
        )
        return _error_result(
            identity,
            context_source,
            failure.stage,
            code,
            cleanup_diagnostics=cleanup_diagnostics or None,
        )
    except Exception as exc:
        logger.warning(
            "Avatar preview evaluation failed stage=%s error_type=%s",
            stage,
            type(exc).__name__,
        )
        invalid = isinstance(exc, avatar_emotion.InvalidEmotionLabel)
        return _error_result(
            identity,
            context_source,
            stage,
            type(exc).__name__,
            logical_call_count=int(model_started),
            raw_business_value=exc.raw_business_value if invalid else None,
            cleanup_diagnostics=cleanup_diagnostics or None,
        )


def create_app(
    config: PreviewConfig | Mapping[str, Any] | None,
    *,
    environ: Mapping[str, str] | None = None,
    conn_factory: Callable[..., Awaitable[Any]] | None = None,
    evaluator: Callable[..., Awaitable[dict[str, Any]]] | None = None,
) -> FastAPI:
    settings = (
        config
        if isinstance(config, PreviewConfig) or config is None
        else PreviewConfig.model_validate(config)
    )
    selected_environment = os.environ if environ is None else environ
    api_key = selected_environment.get(settings.api_key_env) if settings else None
    resolved_db_dsn = (
        selected_environment.get(settings.db_dsn_env)
        if settings and settings.db_dsn_env
        else None
    )
    registry = Evaluations()
    connection_factory = conn_factory or _connect
    emotion_evaluator = evaluator or avatar_emotion.evaluate

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        await registry.close()

    app = FastAPI(title="Hexis Avatar Preview API", lifespan=lifespan)
    app.state.preview_config = settings
    app.state.evaluations = registry

    @app.get("/status")
    async def status() -> dict[str, Any]:
        return {
            "configured": settings is not None,
            "provider": settings.provider if settings else None,
            "model": settings.model if settings else None,
            "credential_available": bool(api_key and api_key.strip()),
            "context_sources": {
                "fixture": bool(settings),
                "db": bool(settings and settings.db_dsn_env and resolved_db_dsn),
            },
            "wordset_version": avatar_emotion.WORDSET_VERSION,
            "prompt_version": avatar_emotion.PROMPT_VERSION,
            "business_budget": "none",
            "message": (
                "离线预览可继续使用；真实评估来源按已选配置显示。"
                if settings
                else "尚未加载真实评估配置；离线预览仍可使用。"
            ),
        }

    @app.post("/evaluate")
    async def evaluate_endpoint(request: EvaluationRequest) -> dict[str, Any]:
        body = request.model_dump(mode="json")

        async def operation() -> dict[str, Any]:
            return await run_evaluation(
                body,
                settings,
                api_key=api_key,
                resolved_db_dsn=resolved_db_dsn,
                conn_factory=connection_factory,
                evaluator=emotion_evaluator,
            )

        return await registry.get_or_start(body, operation)

    @app.get(
        "/evaluations/{session_id}/{input_turn_id}/{revision}", response_model=None
    )
    async def query_evaluation(
        session_id: UUID,
        input_turn_id: UUID,
        revision: int = ApiPath(ge=0),
    ) -> dict[str, Any] | JSONResponse:
        result = await registry.query(session_id, input_turn_id, revision)
        if result is None:
            return JSONResponse(
                status_code=404,
                content={
                    "status": "unknown",
                    "message": "当前服务进程未找到该输入身份；不会自动重新调用模型。",
                },
            )
        return result

    return app


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="启动 loopback Avatar 预览 API")
    parser.add_argument("--config", required=True, help="明确选择的 JSON 配置文件")
    parser.add_argument("--port", required=True, type=int, help="本机显式监听端口")
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("--port 必须在 1 到 65535 之间")
    settings = load_config(args.config)
    selected = _selected_environment(
        settings,
        os.environ,
        PROJECT_ROOT / ".env",
    )
    uvicorn.run(
        create_app(settings, environ=selected),
        host="127.0.0.1",
        port=args.port,
        workers=1,
    )


if __name__ == "__main__":
    main()
