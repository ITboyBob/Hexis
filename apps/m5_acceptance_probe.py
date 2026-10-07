"""M5 持续维护的隔离验收入口；素材、凭据和单次证据留在仓库外。"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit
from uuid import UUID, uuid4
from datetime import datetime, timezone

import asyncpg
from dotenv import dotenv_values


PROFILE_KEYS = (
    "agent.init_profile", "agent.mode", "agent.timezone",
    "llm.chat", "llm.heartbeat", "llm.subconscious",
)
SECRET_KEYS = {"api_key", "password", "token", "access_token", "refresh_token"}
DATABASE_KEYS = ("POSTGRES_HOST", "POSTGRES_PORT", "POSTGRES_USER", "POSTGRES_PASSWORD")


class ProbeError(RuntimeError):
    """验收前置或证据不成立；不自动重试业务动作。"""


def _object(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


def _absolute(value: str | Path) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise ProbeError("必须明确选择绝对路径")
    return path


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _verified_file(reference: dict[str, Any]) -> Path:
    path = _absolute(reference["path"])
    if not path.is_file() or _digest(path) != reference.get("sha256"):
        raise ProbeError(f"文件缺失或哈希不符：{path}")
    return path


def _deny_inline_secrets(value: Any) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if key.lower() in SECRET_KEYS and item:
                raise ProbeError("配置只能引用凭据环境变量，禁止内联凭据")
            _deny_inline_secrets(item)
    elif isinstance(value, list):
        for item in value:
            _deny_inline_secrets(item)


def _selected_env(config: dict[str, Any]) -> dict[str, str]:
    values = dotenv_values(_absolute(config["database"]["env_file"]), interpolate=False)
    keys = (*DATABASE_KEYS, "POSTGRES_DB", config["_realtime"]["api_key_env"],
            config["_gate"]["emotion"]["api_key_env"], config["report"]["api_key_env"])
    if any(not values.get(key) for key in keys):
        raise ProbeError("已选环境文件缺少数据库或模型凭据引用，不能消费ambient登录")
    return {key: str(values[key]) for key in keys}


def load_selected_config(path: str | Path) -> dict[str, Any]:
    """只读取显式选中的配置及其环境文件，不回退默认模板。"""
    selected = _absolute(path)
    if not selected.is_file():
        raise ProbeError("已选配置文件不存在")
    try:
        config = json.loads(selected.read_text())
        if config.get("schema") != "m5-selected-runtime-v1":
            raise ProbeError("所选M5配置版本不受支持")
        _deny_inline_secrets(config)
        _absolute(config["source_root"])
        for key in ("realtime", "input_gate", "assets_manifest"):
            _verified_file(config[key])
        config["_realtime"] = json.loads(_absolute(config["realtime"]["path"]).read_text())
        config["_gate"] = json.loads(_absolute(config["input_gate"]["path"]).read_text())
        _deny_inline_secrets(config["_realtime"])
        _deny_inline_secrets(config["_gate"])
        report = config["report"]
        emotion = config["_gate"]["emotion"]
        if any(report.get(key) != emotion.get(key) for key in ("provider", "model", "endpoint", "api_key_env")):
            raise ProbeError("报告模型必须复用已选qwen-plus配置")
        timeout = report.get("timeout_seconds")
        if (report.get("model") != "qwen-plus" or report.get("provider") != "openai-chat-completions-endpoint"
                or not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0
                or report.get("timeout_source") != "realtime.timeout"
                or timeout != config["_realtime"].get("timeout")):
            raise ProbeError("报告请求必须显式选择模型、endpoint和可溯源超时")
        UUID(config["database"]["consent_log_id"])
        if not config["database"]["profile_source_database"]:
            raise ProbeError("缺少明确profile来源")
        for key in ("api_url", "ui_url"):
            parsed = urlsplit(config[key])
            if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or not parsed.port:
                raise ProbeError("验收API／UI必须显式绑定本地端口")
        if config["api_url"] == config["ui_url"]:
            raise ProbeError("API与UI必须分别选择端口")
        config["_selected_path"] = str(selected)
        values = _selected_env(config)
        if config["database"]["main_database"] != values["POSTGRES_DB"]:
            raise ProbeError("已选主库身份与环境文件不一致")
        if config["database"]["admin_database"] == values["POSTGRES_DB"]:
            raise ProbeError("管理连接禁止指向主库")
        return config
    except (KeyError, TypeError, ValueError) as exc:
        raise ProbeError("所选配置字段缺失或不合法") from exc


def assert_isolated_database(database: str, main_database: str) -> None:
    if database == main_database or not re.fullmatch(r"m5_acceptance_[0-9a-f]{32}", database):
        raise ProbeError("禁止指向主库；只接受本入口生成的独立验收库")


def _dsn(config: dict[str, Any], database: str) -> str:
    values = _selected_env(config)
    return (f"postgresql://{quote(values['POSTGRES_USER'], safe='')}:"
            f"{quote(values['POSTGRES_PASSWORD'], safe='')}@"
            f"{values['POSTGRES_HOST']}:{int(values['POSTGRES_PORT'])}/{quote(database, safe='')}")


def verify_assets(config: dict[str, Any]) -> dict[str, Any]:
    manifest = json.loads(_verified_file(config["assets_manifest"]).read_text())
    references = [*manifest.get("audio", []), *manifest.get("data_files", [])]
    references += [manifest[key] for key in ("contract", "m4_manifest") if key in manifest]
    if not references:
        raise ProbeError("素材清单为空")
    for reference in references:
        _verified_file(reference)
    return {"verified_asset_count": len(references), "manifest_sha256": config["assets_manifest"]["sha256"]}


def redact_evidence(value: Any, config: dict[str, Any]) -> Any:
    values = _selected_env(config)
    secrets = [values["POSTGRES_PASSWORD"], *(v for k, v in values.items() if k not in DATABASE_KEYS and k != "POSTGRES_DB")]
    if isinstance(value, dict):
        return {key: "[redacted]" if key.lower() in SECRET_KEYS else redact_evidence(item, config)
                for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_evidence(item, config) for item in value]
    if isinstance(value, str):
        for secret in secrets:
            value = value.replace(secret, "[redacted]").replace(quote(secret, safe=""), "[redacted]")
    return value


def _write(path: Path, value: Any, config: dict[str, Any]) -> None:
    _absolute(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(redact_evidence(value, config), ensure_ascii=False, indent=2, default=str))


def _fingerprints(root: Path) -> dict[str, str]:
    files = {root / "apps/hexis_api.py", root / "apps/m5_acceptance_probe.py"}
    files.update((root / "hexis-ui/e2e").glob("m5*.spec.ts"))
    files.add(root / "hexis-ui/e2e/fixtures/voice.ts")
    for pattern in ("services/m5*.py", "services/background*.py", "services/voice*.py",
                    "services/qwen_realtime*.py", "services/lightweight_memory_executor.py",
                    "services/prompts/*m5*", "db/migrations/026*.sql",
                    "hexis-ui/app/chat/*.tsx", "hexis-ui/lib/voice-realtime/*.ts", "hexis-ui/lib/m5-tasks.ts"):
        files.update(root.glob(pattern))
    return {str(path.relative_to(root)): _digest(path) for path in sorted(files) if path.is_file()}


def load_run(path: str | Path) -> tuple[dict[str, Any], dict[str, Any]]:
    run = json.loads(_absolute(path).read_text())
    config = load_selected_config(run["selected_config"]["path"])
    if _digest(Path(run["selected_config"]["path"])) != run["selected_config"]["sha256"]:
        raise ProbeError("本次所选配置已变化")
    assert_isolated_database(run["database"], config["database"]["main_database"])
    return run, config


def runtime_dsn(path: str | Path) -> str:
    run, config = load_run(path)
    return _dsn(config, run["database"])


def build_child_environment(config: dict[str, Any], run: dict[str, Any], path: Path) -> dict[str, str]:
    assert_isolated_database(run["database"], config["database"]["main_database"])
    env = dict(os.environ)
    for key in ("HEXIS_INSTANCE", "HEXIS_CONSENT_MOCK", "HEXIS_TEST_CONSENT_DECISION"):
        env.pop(key, None)
    env.update(_selected_env(config))
    env.update({"POSTGRES_DB": run["database"], "HEXIS_DATABASE_URL": _dsn(config, run["database"]),
                "HEXIS_API_URL": config["api_url"], "HEXIS_BROWSER_API_URL": config["api_url"],
                "HEXIS_UI_ORIGIN": config["ui_url"], "HEXIS_CORS_ORIGINS": config["ui_url"],
                "HEXIS_M4_VOICE_CONFIG": config["input_gate"]["path"],
                "HEXIS_REALTIME_CONFIG": config["realtime"]["path"],
                "HEXIS_M5_ACCEPTANCE_RUN": str(_absolute(path))})
    return env


async def prepare_isolated(selected: str | Path, output: str | Path) -> dict[str, Any]:
    config = load_selected_config(selected)
    assets = verify_assets(config)
    path = _absolute(output)
    if path.exists():
        raise ProbeError("运行清单已存在，禁止自动覆盖或重建已有验收库")
    source = config["database"]["profile_source_database"]
    source_conn = await asyncpg.connect(_dsn(config, source))
    try:
        async with source_conn.transaction(readonly=True):
            if await source_conn.fetchval("SELECT current_database()") != source:
                raise ProbeError("profile来源绑定错误")
            status = _object(await source_conn.fetchval("SELECT get_agent_status()"))
            if not status.get("configured"):
                raise ProbeError("所选profile来源尚未有效配置")
            profiles = {row["key"]: _object(row["value"]) for row in await source_conn.fetch(
                "SELECT key,value FROM config WHERE key=ANY($1::text[])", list(PROFILE_KEYS))}
            if set(profiles) != set(PROFILE_KEYS):
                raise ProbeError("缺少必要profile／模型配置")
            _deny_inline_secrets(profiles)
            consent = await source_conn.fetchrow("SELECT * FROM consent_log WHERE id=$1", UUID(config["database"]["consent_log_id"]))
            chat = profiles["llm.chat"]
            if consent is None or consent["decision"] != "consent" or any(
                    consent[key] != chat.get(key) for key in ("provider", "model", "endpoint")):
                raise ProbeError("同意记录不属于所选模型profile")
            payload = _object(consent["response"])
            _deny_inline_secrets(payload)
    finally:
        await source_conn.close()
    run_id = uuid4().hex
    database = "m5_acceptance_" + run_id
    assert_isolated_database(database, config["database"]["main_database"])
    root = Path(config["source_root"])
    run = {"schema": "m5-acceptance-run-v1", "run_id": run_id, "database": database,
           "state": "preparing", "source_root": str(root),
           "selected_config": {"path": config["_selected_path"], "sha256": _digest(Path(config["_selected_path"]))},
           "api_url": config["api_url"], "ui_url": config["ui_url"], "assets": assets,
           "source_fingerprints": _fingerprints(root),
           "head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip(),
           "profile_provenance": {"database": source, "keys": list(PROFILE_KEYS), "consent_log_id": str(consent["id"]),
                                  "operator_override": bool(payload.get("operator_override"))}}
    _write(path, run, config)
    admin = await asyncpg.connect(_dsn(config, config["database"]["admin_database"]))
    try:
        await admin.execute(f'CREATE DATABASE "{database}"')
    finally:
        await admin.close()
    try:
        conn = await asyncpg.connect(_dsn(config, database), command_timeout=120)
        try:
            if await conn.fetchval("SELECT current_database()") != database:
                raise ProbeError("隔离库绑定错误")
            for file in sorted((root / "db").glob("*.sql")):
                await conn.execute(file.read_text())
            from core.migrations import apply_pending_migrations
            run["migrations_applied"] = await apply_pending_migrations(conn)
            async with conn.transaction():
                for key, value in profiles.items():
                    await conn.execute("SELECT set_config($1,$2::jsonb)", key, json.dumps(value))
                await conn.execute("SELECT set_config('llm.m5_report',$1::jsonb)", json.dumps(config["report"]))
                # 原样复用有效同意／操作者覆写，并保留原记录来源；不请求或伪造新的模型同意。
                payload = dict(payload) | {"acceptance_reused_consent": run["profile_provenance"]}
                run["consent_reused"] = _object(await conn.fetchval("SELECT init_consent($1::jsonb)", json.dumps(payload)))
                await conn.execute("UPDATE heartbeat_state SET is_paused=true WHERE id=1")
                origin = _object(await conn.fetchval(
                    "SELECT get_or_create_chat_session(NULL::uuid,'web',NULL::text,$1::jsonb)",
                    json.dumps({"source": "web", "created_by": "m5-acceptance", "m5_run_id": run_id})))
                run["origin_session_id"] = origin["session_id"]
                run["agent_status"] = _object(await conn.fetchval("SELECT get_agent_status()"))
                if not run["agent_status"].get("configured"):
                    raise ProbeError("必要初始化身份未对齐，禁止进入真云")
            run["state"] = "prepared"
        finally:
            await conn.close()
    except Exception as exc:
        run["state"] = "prepare_failed"
        run["failure"] = str(exc)
        _write(path, run, config)
        raise ProbeError("隔离准备失败；数据库与证据保留，见运行清单") from exc
    _write(path, run, config)
    return run


def validate_ui_origin(payload: dict[str, Any], run: dict[str, Any]) -> None:
    # 既有hydrate接口在目标不存在时也返回200；surface只由真实存在行返回。
    if payload.get("session_id") != run["origin_session_id"] or payload.get("surface") != "web":
        raise ProbeError("UI未读取到本次真实来源会话，不能证明Prisma绑定")


async def check_readiness(path: str | Path) -> dict[str, Any]:
    run, config = load_run(path)
    verify_assets(config)
    for relative, expected in run["source_fingerprints"].items():
        if _digest(Path(run["source_root"]) / relative) != expected:
            raise ProbeError(f"服务源码指纹已变化：{relative}")
    conn = await asyncpg.connect(_dsn(config, run["database"]))
    try:
        status = _object(await conn.fetchval("SELECT get_agent_status()"))
        if await conn.fetchval("SELECT current_database()") != run["database"] or not status.get("configured"):
            raise ProbeError("隔离数据库或有效身份不一致")
    finally:
        await conn.close()
    import httpx
    async with httpx.AsyncClient(timeout=config["report"]["timeout_seconds"]) as client:
        health = await client.get(config["api_url"] + "/health")
        health.raise_for_status()
        identity = health.json().get("m5_acceptance", {})
        if (identity.get("database") != run["database"] or identity.get("run_id") != run["run_id"]
                or identity.get("source_fingerprints") != run["source_fingerprints"]
                or not identity.get("task_worker_running") or not identity.get("lightweight_worker_running")):
            raise ProbeError("API进程绑定或真实派发就绪事实不足")
        ui = await client.get(config["ui_url"] + "/api/chat/session/" + run["origin_session_id"])
        ui.raise_for_status()
        validate_ui_origin(ui.json(), run)
    evidence = {"state": "ready", "run_id": run["run_id"], "database": run["database"], "configured": True,
                "api_identity": identity, "ui_origin_readable": True, "credentials_present": True}
    _write(Path(path).parent / "readiness.json", evidence, config)
    return evidence


def acceptance_connect_factory(path: str | Path, connect_factory: Any) -> Any:
    """复用既有EvidenceWebSocket，只观察真实供应商收发，不替代模型。"""
    from apps.voice_realtime_harness import EvidenceWebSocket

    run, config = load_run(path)
    directory = Path(path).parent / "cloud"

    class RecordedSocket(EvidenceWebSocket):
        def __init__(self, socket: Any, endpoint: str):
            super().__init__(socket, protocol_events=[], unknown_events=[], source="real")
            self.record = {"run_id": run["run_id"], "database": run["database"],
                           "source": "real", "endpoint": endpoint, "connection_id": uuid4().hex,
                           "raw_events": [], "protocol_events": self.protocol_events,
                           "unknown_events": self.unknown_events}

        def observe(self, message: str, direction: str) -> None:
            value = json.loads(message)
            if value.get("type") in {
                "session.update", "session.updated", "response.create", "response.created",
                "response.function_call_arguments.done", "response.done", "response.failed",
                "conversation.item.create", "conversation.item.input_audio_transcription.completed",
                "input_audio_transcription.completed", "error",
            }:
                self.record["raw_events"].append({"direction": direction,
                    "observed_at": datetime.now(timezone.utc).isoformat(), "raw": value})
                self.flush()

        def flush(self) -> None:
            _write(directory / (self.record["connection_id"] + ".json"), self.record, config)

        async def send(self, message: str) -> None:
            await super().send(message)
            self.observe(message, "outgoing")

        async def recv(self) -> str:
            message = await super().recv()
            self.observe(message, "incoming")
            return message

        async def close(self) -> None:
            try:
                await super().close()
            finally:
                self.flush()

    async def connect(endpoint: str, *, extra_headers: dict[str, str]) -> Any:
        return RecordedSocket(await connect_factory(endpoint, extra_headers=extra_headers), endpoint)

    return connect


def acceptance_report_request(path: str | Path) -> Any:
    """观察已有独立报告适配器的真实请求和原始返回，不改调用／重试语义。"""
    from services.m5_report_model import request_report_once

    run, selected = load_run(path)

    async def request(config: dict[str, Any], messages: list[dict[str, Any]], tool: dict[str, Any]) -> dict[str, Any]:
        record = {"run_id": run["run_id"], "database": run["database"], "source": "real",
                  "started_at": datetime.now(timezone.utc).isoformat(),
                  "request": {"model": config["model"], "messages": messages, "tools": [tool],
                              "tool_choice": {"type": "function", "function": {"name": "report_task_progress"}},
                              "parallel_tool_calls": False, "stream": False, "enable_thinking": False}}
        output = Path(path).parent / "reports" / (uuid4().hex + ".json")
        _write(output, record, selected)
        try:
            raw = await request_report_once(config, messages, tool)
            record["response"] = raw
            return raw
        except Exception as exc:
            record["failure"] = type(exc).__name__
            raise
        finally:
            record["finished_at"] = datetime.now(timezone.utc).isoformat()
            _write(output, record, selected)

    return request


def verify_case_evidence(case: str, evidence: dict[str, Any], run: dict[str, Any]) -> dict[str, Any]:
    """校验实际call、原回执与持久状态；预期参数、mock或语气不构成事实。"""
    from services.m5_tool_contracts import parse_tool_arguments, build_receipt

    def require(condition: Any, message: str) -> None:
        if not condition:
            raise ProbeError(f"{case}：{message}")

    try:
        tool = {"C1-submit": "submit", "C2-pend_message": "pend_message", "C3-remember": "remember",
                "C4-add_evidence": "add_evidence", "C5-report_task_progress": "report_task_progress"}[case]
        require(evidence.get("source") == "real" and evidence["cloud"].get("source") == "real", "缺少真实供应商收发")
        require(evidence["run_id"] == run["run_id"] and evidence["database"] == run["database"], "运行身份不一致")
        origin = evidence["origin_session_id"]
        after, before = evidence["after"], evidence["before"]
        call = evidence["cloud"]["call"]
        call_id = call["call_id"]
        require(isinstance(call_id, str) and call_id.strip() and not call_id.startswith(("fixture-", "mock-", "test-")), "实际call缺失或为夹具")
        require(call["name"] == tool and evidence["cloud"]["request"], "实际工具与请求缺失")
        arguments = parse_tool_arguments(tool, call["arguments"])
        if tool == "report_task_progress":
            request = evidence["cloud"]["request"]
            require(request["model"] == "qwen-plus" and request["enable_thinking"] is False
                    and len(request["tools"]) == 1 and request["tools"][0]["function"]["name"] == tool
                    and request["parallel_tool_calls"] is False, "独立only-report非思考请求不成立")
            report = next((r for r in after["reports"] if r["last_call_id"] == call_id), None)
            require(report and report["outcome"] == "delivered" and report["origin_session_id"] == origin, "报告未在原来源交付")
            receipt = report["receiver_receipt"]
            require(receipt["actual_call_id"] == call_id and receipt["origin_session_id"] == origin
                    and receipt["report_id"] == report["report_id"] and receipt["task_id"] == report["task_id"], "接收回执归属不一致")
            message = next((m for m in after["messages"] if m["id"] == receipt["message_id"]), None)
            require(message and message["session_id"] == origin and message["content"] == arguments["summary"], "原会话无实际摘要消息")
            work = next(w for w in after["work"] if w["id"] == report["work_execution_id"])
            require(work["status"] == "completed" and datetime.fromisoformat(work["finished_at"]) <= datetime.fromisoformat(evidence["cloud"]["started_at"]), "报告不在真实工作结束之后")
            require(sum(w["id"] == work["id"] for w in after["work"]) == 1, "工作身份重复")
        else:
            receipt = evidence["receipt"]
            build_receipt(**{key: receipt[key] for key in ("tool_name", "call_id", "operation_id", "ok", "state", "data", "error")})
            require(receipt["tool_name"] == tool and receipt["call_id"] == call_id and receipt["ok"] is True, "无成功宿主回执")
            action = next((a for a in after["actions"] if a["actual_call_id"] == call_id), None)
            require(action and action["tool_name"] == tool and action["origin_session_id"] == origin
                    and action["command_id"] == receipt["operation_id"], "DB操作与真实call不匹配")
            authorization = action["authorization"]
            require(call["response_id"] == authorization["supplier_response_id"], "供应商响应未绑定授权")
            require(any(e["event_type"] == "voice_input_committed" and e["input_turn_id"] == authorization["input_turn_id"] for e in after["events"]), "缺少正式Talk输入授权")
            if tool in {"submit", "pend_message"}:
                task = next(t for t in after["tasks"] if t["id"] == receipt["data"]["task_id"])
                require(task["origin_session_id"] == origin, "任务跨来源")
                if tool == "submit":
                    require(receipt["state"] == "accepted" and task["submit_command_id"] == action["command_id"]
                            and task["task_description"] == arguments["task_description"], "submit受理或原参数未保持")
                    require(re.search(r"(?:不|禁止|不要).{0,4}写.{0,4}文件", task["task_description"])
                            and re.search(r"(?:不|禁止|不要).{0,4}联网", task["task_description"]), "原约束丢失")
                    work = next(w for w in after["work"] if w["task_id"] == task["id"] and w["message_id"] is None)
                    text = work["result"].get("visible_text") or work["result"].get("text", "")
                    require("104" in text and re.search(r"3\s*[×x*]\s*18\s*[+＋]\s*2\s*[×x*]\s*25", text), "工作结果不是104元及指定算式")
                else:
                    require(receipt["state"] == "pending", "确认前冒称pending或取消当成功")
                    pending = next(p for p in after["pending"] if p["id"] == receipt["data"]["message_id"])
                    require(pending["command_id"] == action["command_id"] and pending["task_id"] == task["id"]
                            and pending["message"] == arguments["message"], "追加消息非真实持久保存")
                    require(any(t["id"] == task["id"] for t in before["tasks"]), "C2未复用已有任务")
                    work = next(w for w in after["work"] if w["id"] == pending["claimed_work_execution_id"])
                    require(work["message_id"] == pending["id"], "未真实单条领取续轮")
                require(work["status"] == "completed" and work["turn_id"], "没有真实AgentLoop完成")
                require(any(a["session_id"] == task["task_session_id"] and a["decision_kind"] == "background_default" for a in after["audit"]), "后台技能准备审计缺失")
                require(any(m["session_id"] == task["task_session_id"] and m["role"] == "assistant" for m in after["messages"]), "专属会话缺实际工作正文")
            else:
                require(receipt["state"] == "accepted" and "memory_id" not in receipt["data"], "accepted与done混淆")
                require(action["state"] == "done" and action["arguments"] == arguments, "未执行原始实际参数")
                require({t["id"] for t in after["turns"]} == {t["id"] for t in before["turns"]}
                        and {t["id"] for t in after["tasks"]} == {t["id"] for t in before["tasks"]}, "轻量工具错误进入任务或AgentLoop")
                done = action["result"]
                require(done["state"] == "done" and done["ok"] is True
                        and done["operation_id"] == action["command_id"], "缺少最终done或操作归属错误")
                for key, expected in (("call_id", call_id), ("tool_name", tool), ("contract_version", "m5-tool-v1")):
                    require(key not in done or done[key] == expected, "最终done外层身份不一致")
                # S8内部存储只保留业务体；身份来自同一真实动作行，模型回执仍按冻结构造器校验。
                build_receipt(tool_name=tool, call_id=action["actual_call_id"], operation_id=done["operation_id"],
                              ok=done["ok"], state=done["state"], data=done["data"], error=done["error"])
                memory = next(m for m in after["memories"] if m["id"] == done["data"]["memory_id"])
                require(memory["type"] == "semantic", "目标不是实际semantic记忆")
                if tool == "remember":
                    require(memory["content"] == arguments["content"] and "澄湾" in memory["content"], "记忆正文不一致")
                    require(memory["id"] not in {m["id"] for m in before["memories"]}, "没有真实记忆前后变化")
                else:
                    require(arguments["memory_id"] == memory["id"] and arguments["source"]["kind"] == "local_document", "证据目标或来源不一致")
                    require(any(m["id"] == memory["id"] for m in before["memories"]), "证据目标不在此前实际记忆中")
                    context = evidence["evidence_context"]
                    require(context["memory_id"] == memory["id"] and context["source"]["ref"] == arguments["source"]["ref"]
                            and context["source_sha256"] == _digest(Path(context["source"]["ref"])), "C4上下文不可溯源")
                    revisions = [r for r in after["belief_audit"] if r["memory_id"] == memory["id"]
                                 and r["evidence"].get("ref") == arguments["source"]["ref"]]
                    require(revisions and any(r["prior_confidence"] == done["data"]["prior_confidence"]
                                              and r["posterior_confidence"] == done["data"]["posterior_confidence"] for r in revisions), "证据关联或prior/posterior缺失")
        return {"case": case, "passed": True, "call_id": call_id, "origin_session_id": origin}
    except ProbeError:
        raise
    except (KeyError, StopIteration, TypeError, ValueError) as exc:
        raise ProbeError(f"{case}：实际请求／call／回执／DB证据缺失或不一致") from exc


async def collect_snapshot(path: str | Path, origin_session_id: str) -> dict[str, Any]:
    run, config = load_run(path)
    origin = UUID(origin_session_id)
    conn = await asyncpg.connect(_dsn(config, run["database"]))
    try:
        await conn.set_type_codec("jsonb", schema="pg_catalog", encoder=json.dumps, decoder=json.loads)
        async with conn.transaction(isolation="repeatable_read", readonly=True):
            if await conn.fetchval("SELECT current_database()") != run["database"]:
                raise ProbeError("快照库身份不一致")
            if not await conn.fetchval("SELECT EXISTS(SELECT 1 FROM chat_sessions WHERE id=$1 AND status='active' AND surface='web')", origin):
                raise ProbeError("本次来源会话不存在")
            task_sessions = "SELECT task_session_id FROM m5_tasks WHERE origin_session_id=$1"
            queries = {
                "actions": "SELECT * FROM m5_actions WHERE origin_session_id=$1 ORDER BY created_at",
                "tasks": "SELECT * FROM m5_tasks WHERE origin_session_id=$1 ORDER BY created_at",
                "pending": "SELECT p.* FROM m5_pending_messages p JOIN m5_tasks t ON t.id=p.task_id WHERE t.origin_session_id=$1 ORDER BY p.receive_order",
                "work": "SELECT w.* FROM m5_work_rounds w JOIN m5_tasks t ON t.id=w.task_id WHERE t.origin_session_id=$1 ORDER BY w.started_at",
                "reports": "SELECT * FROM m5_reports WHERE origin_session_id=$1 ORDER BY updated_at",
                "selections": "SELECT * FROM m5_selections WHERE origin_session_id=$1 ORDER BY created_at",
                "events": f"SELECT * FROM behavior_events WHERE session_id=$1 OR session_id IN ({task_sessions}) ORDER BY ts,seq",
                "turns": f"SELECT * FROM agent_turns WHERE session_id IN ({task_sessions}) ORDER BY created_at",
                "audit": f"SELECT * FROM tool_surface_decision_events WHERE session_id IN ({task_sessions}) ORDER BY created_at",
                "messages": f"SELECT * FROM chat_messages WHERE session_id=$1 OR session_id IN ({task_sessions}) ORDER BY created_at",
                "voice": "SELECT * FROM voice_realtime_sessions WHERE session_id=$1 ORDER BY created_at",
            }
            result = {"database": run["database"], "origin_session_id": str(origin)}
            for key, query in queries.items():
                result[key] = [dict(row) for row in await conn.fetch(query, origin)]
            result["memories"] = [dict(row) for row in await conn.fetch("SELECT id,type,status,content,metadata,created_at,updated_at FROM memories WHERE type='semantic' ORDER BY created_at")]
            result["belief_audit"] = [dict(row) for row in await conn.fetch("SELECT * FROM belief_revision_audit ORDER BY created_at")]
            result["memory_edges"] = [dict(row) for row in await conn.fetch("SELECT * FROM memory_edges WHERE src_type='memory' AND dst_type='memory'")]
        return json.loads(json.dumps(redact_evidence(result, config), default=str))
    finally:
        await conn.close()


async def bind_evidence_context(path: str | Path, origin_session_id: str) -> dict[str, Any]:
    run, config = load_run(path)
    await collect_snapshot(path, origin_session_id)  # 当前正式输入来源也必须真实存在。
    verified = json.loads((Path(path).parent / "verification/C3-remember.json").read_text())
    if verified["result"].get("passed") is not True or verified["evidence"]["run_id"] != run["run_id"]:
        raise ProbeError("C4必须复用本运行已验证C3的真实记忆")
    remember_origin = verified["result"]["origin_session_id"]
    snapshot = await collect_snapshot(path, remember_origin)
    actions = [a for a in snapshot["actions"] if a["tool_name"] == "remember" and a["state"] == "done"
               and a["actual_call_id"] == verified["result"]["call_id"]]
    if len(actions) != 1:
        raise ProbeError("C4必须绑定本次唯一C3实际记忆，不能猜UUID")
    memory_id = actions[0]["result"]["data"]["memory_id"]
    memory = next((m for m in snapshot["memories"] if m["id"] == memory_id and m["status"] == "active"), None)
    if memory is None or memory["type"] != "semantic":
        raise ProbeError("C3实际semantic目标不可证明")
    manifest = json.loads(_verified_file(config["assets_manifest"]).read_text())
    cases_ref = next(r for r in manifest["data_files"] if Path(r["path"]).name == "tool-cases.json")
    cases = json.loads(_verified_file(cases_ref).read_text())["cases"]
    source = next(c for c in cases if c["id"] == "C4-add_evidence")["argument_template"]["source"]
    reference = next(r for r in manifest["data_files"] if r["path"] == source["ref"])
    source_file = _verified_file(reference)
    context = {"run_id": run["run_id"], "database": run["database"], "origin_session_id": origin_session_id,
               "remember_origin_session_id": remember_origin,
               "memory_id": memory_id, "memory_content": memory["content"], "remember_call_id": actions[0]["actual_call_id"],
               "target_memory": {"id": memory_id, "type": memory["type"], "status": memory["status"], "content": memory["content"]},
               "source": source, "source_sha256": _digest(source_file), "source_content": source_file.read_text(),
               "scope": "验收宿主从本次DB和授权缓存提供的受控上下文；不是模型自行读取本地文件"}
    _write(Path(path).parent / "evidence-context.json", context, config)
    return context


def augment_evidence_hydration(path: str | Path, result: Any, session_id: UUID) -> Any:
    context_file = Path(path).parent / "evidence-context.json"
    if not context_file.is_file():
        return result
    run, _ = load_run(path)
    context = json.loads(context_file.read_text())
    if context["origin_session_id"] != str(session_id):
        return result
    if context["run_id"] != run["run_id"] or context["source_sha256"] != _digest(Path(context["source"]["ref"])):
        raise ProbeError("验收上下文来源已变化")
    from dataclasses import replace
    from services.voice_instructions import load_m5_instructions, render_per_turn

    facts = {"target_memory": context["target_memory"], "lookup_kind": "direct_database_id",
             "evidence_source": context["source"], "source_content": context["source_content"],
             "scope": "该active semantic记录是宿主按ID从本次权威DB直接读取的既有记忆；检索无匹配只表示query未命中，不代表此已读取记录不存在。日程正文由授权缓存提供，不是模型自行读文件。"}
    memory_context = result.memory_context + "\n\n## 已直接读取的目标语义记忆与证据来源\n" + json.dumps(facts, ensure_ascii=False)
    rendered = render_per_turn(load_m5_instructions(), now=result.temporal_context,
                              persona=result.persona_context, continuity=result.continuity_context, memory=memory_context)
    return replace(result, per_response_instructions=rendered.text, memory_context=memory_context,
                   token_count=rendered.token_count, tokenizer=rendered.tokenizer, tokenizer_is_model_exact=rendered.tokenizer_is_model_exact)


async def verify_case(path: str | Path, case: str) -> dict[str, Any]:
    run, config = load_run(path)
    directory = Path(path).parent
    case_directory = _absolute(run.get("case_directory", str(directory / "cases")))
    browser_case = json.loads((case_directory / (case + ".json")).read_text())
    if browser_case.get("run_id") != run["run_id"] or browser_case.get("failure") is not None:
        raise ProbeError("浏览器案例失败或不属于本次运行")
    origin = browser_case["origin_session_id"]
    before = browser_case["before"]
    after = await collect_snapshot(path, origin)
    evidence = {"run_id": run["run_id"], "database": run["database"], "origin_session_id": origin,
                "source": "real", "before": before, "after": after, "browser_case": browser_case}
    if case == "C5-report_task_progress":
        found = None
        for file in sorted((directory / "reports").glob("*.json")):
            record = json.loads(file.read_text())
            if record.get("run_id") != run["run_id"] or "response" not in record:
                continue
            calls = record["response"].get("choices", [{}])[0].get("message", {}).get("tool_calls", [])
            for call in calls:
                if any(r["last_call_id"] == call["id"] and r["outcome"] == "delivered" for r in after["reports"]):
                    found = (file, record, call)
                    break
            if found:
                break
        if not found:
            raise ProbeError("缺少独立报告真实响应与delivered关联")
        file, record, call = found
        evidence["cloud"] = {"source": record["source"], "request": record["request"], "started_at": record["started_at"],
                             "call": {"name": call["function"]["name"], "call_id": call["id"], "arguments": call["function"]["arguments"]}}
        evidence["cloud_artifact"] = freeze_case_artifact(file, directory, case)
    else:
        tool = case.split("-", 1)[1]
        before_ids = {a["command_id"] for a in before["actions"]}
        actions = [a for a in after["actions"] if a["tool_name"] == tool and a["command_id"] not in before_ids]
        if len(actions) != 1:
            raise ProbeError(f"{case}必须有一次实际受理，本次新增{len(actions)}项")
        action = actions[0]
        found = None
        for file in sorted((directory / "cloud").glob("*.json")):
            record = json.loads(file.read_text())
            if record.get("run_id") != run["run_id"]:
                continue
            events = record["raw_events"]
            for index, event in enumerate(events):
                raw = event["raw"]
                if raw.get("type") == "response.function_call_arguments.done" and raw.get("call_id") == action["actual_call_id"]:
                    requests = [e["raw"] for e in events[:index] if e["direction"] == "outgoing" and e["raw"].get("type") == "response.create"]
                    outputs = [e["raw"]["item"] for e in events[index:] if e["direction"] == "outgoing"
                               and e["raw"].get("type") == "conversation.item.create"
                               and e["raw"].get("item", {}).get("call_id") == action["actual_call_id"]]
                    if requests and outputs:
                        found = (file, record, raw, requests[-1], _object(outputs[0]["output"]))
                        break
            if found:
                break
        if not found:
            raise ProbeError("缺少真实Realtime请求／工具响应／宿主工具回执关联")
        file, record, call, request, receipt = found
        evidence["cloud"] = {"source": record["source"], "request": request, "call": call}
        evidence["cloud_artifact"] = freeze_case_artifact(file, directory, case)
        evidence["receipt"] = receipt
        if case == "C4-add_evidence":
            evidence["evidence_context"] = json.loads((directory / "evidence-context.json").read_text())
    result = verify_case_evidence(case, evidence, run)
    _write(directory / "verification" / (case + ".json"), {"result": result, "evidence": evidence}, config)
    return result


def freeze_case_artifact(source: Path, directory: Path, case: str) -> dict[str, str]:
    """共享WS日志还会追加；案例引用固定原字节快照，避免后续轮次改变哈希。"""
    data = source.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    target = directory / "cloud-snapshots" / f"{case}-{digest}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        with target.open("xb") as output:
            output.write(data)
    reference = {"path": str(target), "sha256": digest}
    _verified_file(reference)
    return reference


def serve(path: str | Path, surface: str) -> None:
    """仅启动显式所选运行；UI副本排除环境文件，保留日常服务。"""
    run, config = load_run(path)
    env = build_child_environment(config, run, _absolute(path))
    for relative, expected in run["source_fingerprints"].items():
        if _digest(Path(run["source_root"]) / relative) != expected:
            raise ProbeError(f"服务源码指纹已变化：{relative}")
    if surface == "api":
        import sys

        os.chdir(run["source_root"])
        command = [sys.executable, "-m", "apps.hexis_api", "--host", "127.0.0.1", "--port", str(urlsplit(config["api_url"]).port)]
    else:
        ui_root = _absolute(config["ui_source_root"])
        if ui_root == Path(run["source_root"]) / "hexis-ui":
            raise ProbeError("隔离UI禁止覆盖源码或日常UI目录")
        ui_root.mkdir(parents=True, exist_ok=True)
        subprocess.run(["rsync", "-a", "--exclude=node_modules", "--exclude=.next", "--exclude=.env*",
                        str(Path(run["source_root"]) / "hexis-ui") + "/", str(ui_root) + "/"], check=True)
        modules = ui_root / "node_modules"
        if not modules.exists():
            modules.symlink_to(Path(run["source_root"]) / "hexis-ui/node_modules", target_is_directory=True)
        for relative, expected in run["source_fingerprints"].items():
            if relative.startswith("hexis-ui/") and _digest(ui_root / relative.removeprefix("hexis-ui/")) != expected:
                raise ProbeError("隔离UI副本源码与运行清单不一致")
        os.chdir(ui_root)
        command = [str(modules / ".bin/next"), "dev", "--webpack", "--hostname", "127.0.0.1", "--port", str(urlsplit(config["ui_url"]).port)]
    os.execvpe(command[0], command, env)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare-isolated")
    prepare.add_argument("--selected-config", required=True)
    prepare.add_argument("--output", required=True)
    ready = commands.add_parser("check-readiness")
    ready.add_argument("--run", required=True)
    for name in ("serve-api", "serve-ui"):
        commands.add_parser(name).add_argument("--run", required=True)
    snapshot = commands.add_parser("snapshot")
    snapshot.add_argument("--run", required=True)
    snapshot.add_argument("--origin-session-id", required=True)
    snapshot.add_argument("--output")
    context = commands.add_parser("bind-evidence-context")
    context.add_argument("--run", required=True)
    context.add_argument("--origin-session-id", required=True)
    verify = commands.add_parser("verify-case")
    verify.add_argument("--run", required=True)
    verify.add_argument("--case", required=True, choices=("C1-submit", "C2-pend_message", "C3-remember", "C4-add_evidence", "C5-report_task_progress"))
    args = parser.parse_args(argv)
    try:
        if args.command == "prepare-isolated":
            result = asyncio.run(prepare_isolated(args.selected_config, args.output))
        elif args.command == "check-readiness":
            result = asyncio.run(check_readiness(args.run))
        elif args.command in ("snapshot", "bind-evidence-context"):
            if args.command == "snapshot":
                result = asyncio.run(collect_snapshot(args.run, args.origin_session_id))
                if args.output:
                    _, config = load_run(args.run)
                    _write(_absolute(args.output), result, config)
            else:
                result = asyncio.run(bind_evidence_context(args.run, args.origin_session_id))
            print(json.dumps(result, ensure_ascii=False, default=str))
            return 0
        elif args.command == "verify-case":
            print(json.dumps(asyncio.run(verify_case(args.run, args.case)), ensure_ascii=False))
            return 0
        else:
            serve(args.run, args.command.removeprefix("serve-"))
            return 0
        print(json.dumps({key: result[key] for key in ("state", "database")}, ensure_ascii=False))
        return 0
    except ProbeError as exc:
        print(str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
