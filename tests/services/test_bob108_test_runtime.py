"""BOB-108 runner 的离线配置与身份拒绝边界。"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

import httpx

from apps import bob108_test_runtime as runtime
from core import llm
from core.integration_reliability import iter_sse_json_events
from tests.fixtures import bob108_controlled_runtime as controlled


class RuntimeValidationTests(unittest.TestCase):
    def test_control_volume_names_track_repository_postgres_generation(self) -> None:
        volumes = runtime.control_volume_names("hexis-bob108-control")
        self.assertNotEqual(volumes["pytest"], volumes["browser"])
        self.assertTrue(all("brain-pgdata" in name for name in volumes.values()))

    def test_control_databases_reuse_repository_brain_image_and_build(self) -> None:
        image = "ghcr.io/quixiai/hexis-brain:repository-selected"
        configured = CompletedProcess(
            [], 0, json.dumps({"services": {"db": {
                "image": image,
                "build": {"context": str(runtime.REPO_ROOT), "dockerfile": "ops/Dockerfile.db"},
            }}}), "",
        )
        volumes = runtime.control_volume_names("hexis-bob108-control")
        with patch.object(runtime.subprocess, "run", return_value=configured) as compose:
            generated = runtime._compose_text("hexis-bob108-control", runtime.DEFAULT_PORTS, volumes)
        self.assertEqual(generated.count(f"image: {json.dumps(image)}"), 2)
        self.assertEqual(generated.count("dockerfile: \"ops/Dockerfile.db\""), 2)
        self.assertEqual(generated.count("search_path=public,ag_catalog"), 2)
        self.assertEqual(generated.count("app.embedding_service_url=http://fake-embeddings:45435/api/embed"), 2)
        self.assertIn(f"{volumes['pytest']}:/var/lib/postgresql/data", generated)
        self.assertIn(f"{volumes['browser']}:/var/lib/postgresql/data", generated)
        self.assertNotIn("image: postgres:16", generated)
        self.assertIn("--env-file", compose.call_args.args[0])
        self.assertIn("/dev/null", compose.call_args.args[0])
        self.assertNotIn("POSTGRES_PASSWORD", compose.call_args.kwargs["env"])

    def test_repository_postgres_config_rejects_missing_build_configuration(self) -> None:
        configured = CompletedProcess([], 0, json.dumps({"services": {"db": {"image": "postgres:16"}}}), "")
        with patch.object(runtime.subprocess, "run", return_value=configured):
            with self.assertRaisesRegex(ValueError, "image and build"):
                runtime._repository_postgres_config()

    def test_repository_postgres_config_rejects_another_source_root(self) -> None:
        configured = CompletedProcess([], 0, json.dumps({"services": {"db": {
            "image": "brain:owned",
            "build": {"context": "/tmp/other-source", "dockerfile": "ops/Dockerfile.db"},
        }}}), "")
        with patch.object(runtime.subprocess, "run", return_value=configured):
            with self.assertRaisesRegex(ValueError, "source root"):
                runtime._repository_postgres_config()

    def test_run_directory_is_confined_to_the_bob108_cache(self) -> None:
        self.assertEqual(
            runtime.validate_run_dir("/Users/itboybob/.cache/hexis/bob108-20261009/runs/control-01"),
            Path("/Users/itboybob/.cache/hexis/bob108-20261009/runs/control-01"),
        )
        with self.assertRaisesRegex(ValueError, "cache"):
            runtime.validate_run_dir("/tmp/unowned-bob108-run")

    def test_manifest_rejects_wrong_run_or_shared_volume(self) -> None:
        manifest = {
            "run_id": "control-01",
            "project": "hexis-bob108-control",
            "source_root": str(runtime.REPO_ROOT),
            "ports": {"pytest": 45816, "browser": 45815, "embeddings": 45435},
            "volumes": {"pytest": "pytest-data", "browser": "pytest-data"},
        }
        with self.assertRaisesRegex(ValueError, "volume"):
            runtime.validate_control_manifest(manifest)

        manifest["volumes"]["browser"] = "browser-data"
        manifest["run_id"] = "other-run"
        with self.assertRaisesRegex(ValueError, "run_id"):
            runtime.validate_control_manifest(manifest)

    def test_postgres_identities_must_be_distinct_and_expected(self) -> None:
        identities = {
            "pytest": {"database": "bob108_pytest", "system_identifier": "101"},
            "browser": {"database": "bob108_browser", "system_identifier": "101"},
        }
        with self.assertRaisesRegex(RuntimeError, "system identifier"):
            runtime.validate_service_identities(identities)

        identities["browser"]["system_identifier"] = "202"
        with self.assertRaisesRegex(RuntimeError, "database"):
            identities["browser"]["database"] = "hexis_memory"
            runtime.validate_service_identities(identities)

    def test_embedding_health_must_be_a_valid_health_response(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "health"):
            runtime.validate_embedding_health({"error": "unavailable"})
        runtime.validate_embedding_health({"models": []})

    def test_unavailable_ports_fail_before_compose_invocation(self) -> None:
        with patch.object(runtime, "port_is_available", side_effect=[True, False]), patch.object(
            runtime.subprocess, "run"
        ) as run:
            with self.assertRaisesRegex(RuntimeError, "45816"):
                runtime.require_available_ports([45815, 45816])
        run.assert_not_called()


class ControlledProviderTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.provider = controlled.ScriptedProvider()
        self.environment = patch.dict("os.environ", {"BOB108_CONTROLLED_RUNTIME": "1"})
        self.environment.start()
        self.selected = patch.object(controlled, "scripted_provider", self.provider)
        self.selected.start()
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=controlled.provider_app),
            base_url="http://controlled",
        )
        await self.provider.replace([
            {"tool_call": {"id": "recall-1", "name": "recall", "arguments": {"query": "Hexis"}}},
            {"content": "工具执行后回复"},
        ])

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        self.selected.stop()
        self.environment.stop()

    def tool_request(self, *, stream: bool) -> dict:
        return {
            "model": "bob108-scripted",
            "messages": [{"role": "user", "content": "查询 Hexis"}],
            "tools": [{"type": "function", "function": {"name": "recall"}}],
            "stream": stream,
        }

    async def test_auxiliary_json_request_preserves_the_armed_tool_scenario(self) -> None:
        appraisal = await self.client.post("/v1/chat/completions", json={
            "model": "bob108-scripted", "messages": [],
            "response_format": {"type": "json_object"},
        })
        self.assertEqual(appraisal.status_code, 200)
        self.assertEqual(json.loads(appraisal.json()["choices"][0]["message"]["content"]), {})
        recall = await self.client.post("/v1/chat/completions", json=self.tool_request(stream=False))
        self.assertEqual(recall.status_code, 200)
        self.assertEqual(recall.json()["choices"][0]["message"]["tool_calls"][0]["id"], "recall-1")

    async def test_streamed_tool_call_and_reply_have_sse_deltas_and_finish_signals(self) -> None:
        recall = await self.client.post("/v1/chat/completions", json=self.tool_request(stream=True))
        self.assertEqual(recall.status_code, 200)
        self.assertIn("text/event-stream", recall.headers["content-type"])
        events = [json.loads(line[6:]) for line in recall.text.splitlines() if line.startswith("data: ") and line != "data: [DONE]"]
        call = events[0]["choices"][0]["delta"]["tool_calls"][0]
        self.assertEqual(call["index"], 0)
        self.assertEqual(json.loads(call["function"]["arguments"]), {"query": "Hexis"})
        self.assertEqual(events[-1]["choices"][0]["finish_reason"], "tool_calls")
        self.assertTrue(recall.text.endswith("data: [DONE]\n\n"))
        reply = await self.client.post("/v1/chat/completions", json=self.tool_request(stream=True))
        self.assertIn("工具执行后回复", reply.text)
        self.assertIn('"finish_reason": "stop"', reply.text)

    async def test_rejected_tool_request_does_not_consume_the_script(self) -> None:
        rejected = await self.client.post("/v1/chat/completions", json={"messages": [], "tools": []})
        self.assertEqual(rejected.status_code, 422)
        recall = await self.client.post("/v1/chat/completions", json=self.tool_request(stream=False))
        self.assertEqual(recall.status_code, 200)
        self.assertEqual(recall.json()["choices"][0]["finish_reason"], "tool_calls")

    async def test_fixture_stream_is_consumed_by_the_production_llm_parser(self) -> None:
        def local_transport(*args, **kwargs):
            return iter_sse_json_events(
                *args, **kwargs, transport=httpx.ASGITransport(app=controlled.provider_app)
            )

        options = dict(
            provider="openai-chat-completions-endpoint", model="bob108-scripted",
            endpoint="http://controlled/v1", api_key=None,
            messages=[{"role": "user", "content": "查询 Hexis"}],
            tools=self.tool_request(stream=True)["tools"],
            temperature=0, max_tokens=128, on_reasoning_delta=None,
        )
        text: list[str] = []
        with patch.object(llm, "iter_sse_json_events", local_transport):
            recall = await llm._compatible_chat_stream_completion(**options, on_text_delta=text.append)
            self.assertEqual(recall["tool_calls"], [{"id": "recall-1", "name": "recall", "arguments": {"query": "Hexis"}}])
            self.assertEqual(text, [])
            reply = await llm._compatible_chat_stream_completion(**options, on_text_delta=text.append)
        self.assertEqual(reply["content"], "工具执行后回复")
        self.assertEqual(text, ["工具执行后回复"])
        self.assertEqual(reply["tool_calls"], [])

    async def test_provider_is_unavailable_without_the_controlled_flag(self) -> None:
        with patch.dict("os.environ", {"BOB108_CONTROLLED_RUNTIME": "0"}):
            response = await self.client.post("/v1/chat/completions", json=self.tool_request(stream=True))
        self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()
