"""离线验证 BOB-108 runner 的启动顺序与安全边界。"""

from __future__ import annotations

import unittest
import json
import subprocess
import sys
import tempfile
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest.mock import AsyncMock, patch

from apps import bob108_test_runtime


class BootstrapTests(unittest.TestCase):
    def pytest_manifest(self) -> dict:
        return {
            "run_id": "control-01",
            "project": "hexis-bob108-control",
            "source_root": str(bob108_test_runtime.REPO_ROOT),
            "ports": {"pytest": 45816, "browser": 45815, "embeddings": 45435},
            "volumes": bob108_test_runtime.control_volume_names("hexis-bob108-control"),
            "services": ["pytest-db", "browser-db", "fake-embeddings"],
            "ready": True,
            "runtime": {
                "pytest_host": "127.0.0.1", "pytest_port": 45816,
                "pytest_user": "bob108", "pytest_db": "bob108_pytest",
                "pytest_admin_db": "postgres", "embedding_host": "127.0.0.1",
                "embedding_port": 45435, "embedding_dimension": 768,
                "fake_embeddings_url": "http://127.0.0.1:45435/api/embed",
                "embedding_model_id": "bob108-fake-768",
            },
            "service_identities": {
                "pytest": {"database": "bob108_pytest", "system_identifier": "101"},
                "browser": {"database": "bob108_browser", "system_identifier": "202"},
            },
            "owned_resources": {
                service: {"container_id": service + "-owned"}
                for service in ("pytest-db", "browser-db", "fake-embeddings")
            },
        }

    @contextmanager
    def mocked_pytest_runtime(self, manifest: dict):
        with ExitStack() as stack:
            mocks = {}
            replacements = {
                "_load_manifest": {"return_value": manifest},
                "_config_selection": {"return_value": {"credential_env_file": "/selected/.env"}},
                "_read_dotenv": {"return_value": {"POSTGRES_PASSWORD": "owned-password"}},
                "_owned_resources": {"return_value": manifest["owned_resources"]},
                "_pg_identity": {"new_callable": AsyncMock, "return_value": manifest["service_identities"]["pytest"]},
                "_http_health": {"return_value": {"models": []}},
                "_write_manifest": {},
            }
            for name, options in replacements.items():
                mocks[name] = stack.enter_context(patch.object(bob108_test_runtime, name, **options))
            mocks["child"] = stack.enter_context(patch.object(
                bob108_test_runtime.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)
            ))
            yield mocks

    def test_pytest_checks_live_owned_services_before_starting_child(self) -> None:
        manifest = self.pytest_manifest()
        with self.mocked_pytest_runtime(manifest) as mocks:
            result = bob108_test_runtime.pytest_command(Path("/cache/run"), ["--", "tests/services", "-q"])
        self.assertEqual(result, 0)
        mocks["_owned_resources"].assert_called_once()
        mocks["_pg_identity"].assert_awaited_once_with("127.0.0.1", 45816, "bob108_pytest", "bob108", "owned-password")
        mocks["_http_health"].assert_called_once_with("http://127.0.0.1:45435/api/tags")
        self.assertEqual(mocks["child"].call_args.args[0], [sys.executable, "-m", "pytest", "tests/services", "-q"])

    def test_pytest_refuses_unreachable_database_despite_cached_ready(self) -> None:
        manifest = self.pytest_manifest()
        with self.mocked_pytest_runtime(manifest) as mocks:
            mocks["_pg_identity"].side_effect = ConnectionRefusedError("offline")
            with self.assertRaisesRegex(RuntimeError, "readiness"):
                bob108_test_runtime.pytest_command(Path("/cache/run"), ["tests/services"])
        mocks["child"].assert_not_called()
        self.assertFalse(manifest["ready"])
        mocks["_write_manifest"].assert_called_once()

    def test_pytest_refuses_changed_postgres_system_identity(self) -> None:
        manifest = self.pytest_manifest()
        with self.mocked_pytest_runtime(manifest) as mocks:
            mocks["_pg_identity"].return_value = {"database": "bob108_pytest", "system_identifier": "909"}
            with self.assertRaisesRegex(RuntimeError, "identity"):
                bob108_test_runtime.pytest_command(Path("/cache/run"), [])
        mocks["child"].assert_not_called()

    def test_pytest_refuses_changed_owned_container_identity(self) -> None:
        manifest = self.pytest_manifest()
        with self.mocked_pytest_runtime(manifest) as mocks:
            mocks["_owned_resources"].return_value = {
                **manifest["owned_resources"], "pytest-db": {"container_id": "foreign"}
            }
            with self.assertRaisesRegex(RuntimeError, "identity"):
                bob108_test_runtime.pytest_command(Path("/cache/run"), [])
        mocks["child"].assert_not_called()

    def test_pytest_refuses_unhealthy_embeddings(self) -> None:
        manifest = self.pytest_manifest()
        with self.mocked_pytest_runtime(manifest) as mocks:
            mocks["_http_health"].side_effect = ConnectionRefusedError("offline")
            with self.assertRaisesRegex(RuntimeError, "readiness"):
                bob108_test_runtime.pytest_command(Path("/cache/run"), [])
        mocks["child"].assert_not_called()

    def test_pytest_refuses_runtime_bindings_outside_manifest(self) -> None:
        for field, value in (("pytest_port", 43815), ("fake_embeddings_url", "http://ambient/api/embed")):
            with self.subTest(field=field):
                manifest = self.pytest_manifest()
                manifest["runtime"][field] = value
                with self.mocked_pytest_runtime(manifest) as mocks:
                    with self.assertRaisesRegex(RuntimeError, "binding"):
                        bob108_test_runtime.pytest_command(Path("/cache/run"), [])
                mocks["_pg_identity"].assert_not_awaited()
                mocks["child"].assert_not_called()

    def test_stop_owned_invalidates_cached_readiness_even_when_stop_fails(self) -> None:
        for fails in (False, True):
            with self.subTest(fails=fails):
                manifest = self.pytest_manifest()
                manifest["controlled_runtime_ready"] = True
                manifest["controlled_processes"] = {"ui": {"pid": 123}}
                def stop_with_readiness_check(*args, **kwargs):
                    self.assertFalse(manifest["ready"])
                    self.assertFalse(manifest["controlled_runtime_ready"])
                    if fails:
                        raise RuntimeError("stop failure")

                with self.mocked_pytest_runtime(manifest) as mocks, patch.object(
                    bob108_test_runtime, "_stop_controlled_process", side_effect=stop_with_readiness_check,
                ):
                    mocks["child"].return_value = subprocess.CompletedProcess([], 0, stdout="")
                    if fails:
                        with self.assertRaisesRegex(RuntimeError, "stop failure"):
                            bob108_test_runtime.stop_owned(Path("/cache/run"))
                    else:
                        bob108_test_runtime.stop_owned(Path("/cache/run"))
                self.assertFalse(manifest["ready"])
                mocks["_write_manifest"].assert_called()

    def test_stop_controlled_cli_stops_only_owned_pids_and_preserves_database(self) -> None:
        manifest = self.pytest_manifest()
        manifest["controlled_runtime_ready"] = True
        processes = {role: {"pid": pid, "run_id": "control-01"}
                     for role, pid in (("ui", 123), ("api_provider", 456))}
        manifest["controlled_processes"] = processes
        resources = manifest["owned_resources"]
        run_dir = bob108_test_runtime.CACHE_ROOT / "control-01"
        with self.mocked_pytest_runtime(manifest) as mocks, patch.object(
            bob108_test_runtime, "_stop_controlled_process"
        ) as stop:
            self.assertEqual(bob108_test_runtime.main(["stop-controlled", "--run-dir", str(run_dir)]), 0)
        self.assertEqual([call.kwargs["role"] for call in stop.call_args_list], ["ui", "api_provider"])
        self.assertTrue(manifest["ready"])
        self.assertIs(manifest["owned_resources"], resources)
        self.assertFalse(manifest["controlled_runtime_ready"])
        self.assertEqual(manifest["controlled_processes"], {})
        mocks["child"].assert_not_called()
        mocks["_owned_resources"].assert_not_called()
        mocks["_config_selection"].assert_not_called()

    def test_stop_owned_cli_stops_manifest_containers_after_identity_check(self) -> None:
        manifest = self.pytest_manifest()
        run_dir = bob108_test_runtime.CACHE_ROOT / "control-01"
        with self.mocked_pytest_runtime(manifest) as mocks:
            mocks["child"].side_effect = [
                subprocess.CompletedProcess([], 0, stdout="owned-container\n"),
                subprocess.CompletedProcess([], 0),
            ]
            self.assertEqual(bob108_test_runtime.main(["stop-owned", "--run-dir", str(run_dir)]), 0)
        mocks["_owned_resources"].assert_called_once_with(run_dir, manifest)
        self.assertEqual(len(mocks["child"].call_args_list), 2)
        stop_command = mocks["child"].call_args_list[-1].args[0]
        self.assertEqual(stop_command[:4], ["docker", "compose", "--project-name", "hexis-bob108-control"])
        self.assertEqual(stop_command[-1], "stop")
        self.assertFalse(manifest["ready"])
        self.assertEqual(manifest["owned_resources"], {})

    def test_stop_owned_refuses_changed_container_identity(self) -> None:
        manifest = self.pytest_manifest()
        recorded_resources = manifest["owned_resources"]
        with self.mocked_pytest_runtime(manifest) as mocks:
            mocks["child"].return_value = subprocess.CompletedProcess([], 0, stdout="foreign-container\n")
            mocks["_owned_resources"].return_value = {
                **recorded_resources, "browser-db": {"container_id": "foreign"}
            }
            with self.assertRaisesRegex(RuntimeError, "identities differ"):
                bob108_test_runtime.stop_owned(Path("/cache/run"))
        mocks["child"].assert_called_once()
        self.assertFalse(manifest["ready"])
        self.assertIs(manifest["owned_resources"], recorded_resources)

    def test_stop_controlled_retains_process_records_when_pid_guard_fails(self) -> None:
        manifest = self.pytest_manifest()
        processes = {"ui": {"pid": 123, "run_id": "control-01"}}
        manifest["controlled_processes"] = processes
        with self.mocked_pytest_runtime(manifest) as mocks, patch.object(
            bob108_test_runtime, "_stop_controlled_process", side_effect=RuntimeError("PID is foreign")
        ):
            with self.assertRaisesRegex(RuntimeError, "foreign"):
                bob108_test_runtime.stop_controlled(Path("/cache/run"))
        self.assertIs(manifest["controlled_processes"], processes)
        self.assertTrue(manifest["ready"])
        mocks["child"].assert_not_called()
        mocks["_write_manifest"].assert_not_called()

    def test_stop_process_accepts_owned_command_and_ignores_exited_pid(self) -> None:
        run_dir = Path("/cache/run")
        for role, command in (
            ("api_provider", "python -m tests.fixtures.bob108_controlled_runtime --run-dir /cache/run"),
            ("ui", "node /cache/run/ui-src/node_modules/next/dist/bin/next dev"),
        ):
            with self.subTest(role=role), patch.object(
                bob108_test_runtime, "_process_command", side_effect=[command, None]
            ), patch.object(bob108_test_runtime.os, "killpg") as kill:
                bob108_test_runtime._stop_controlled_process(
                    {"pid": 123, "run_id": "control-01"}, run_dir=run_dir, role=role
                )
                kill.assert_called_once_with(123, bob108_test_runtime.signal.SIGTERM)
        with patch.object(bob108_test_runtime, "_process_command", return_value=None), patch.object(
            bob108_test_runtime.os, "killpg"
        ) as kill:
            bob108_test_runtime._stop_controlled_process(
                {"pid": 123, "run_id": "control-01"}, run_dir=run_dir, role="ui"
            )
        kill.assert_not_called()

    def test_stop_process_refuses_foreign_command_or_run_before_signal(self) -> None:
        for role, command, run_id in (
            ("ui", "node /another/run/ui-src/node_modules/next/dist/bin/next dev", "control-01"),
            ("api_provider", "python -m unrelated_runtime", "control-01"),
            ("api_provider", "python -m tests.fixtures.bob108_controlled_runtime --run-dir /another/run", "control-01"),
            ("api_provider", "python -m tests.fixtures.bob108_controlled_runtime --run-dir /cache/run-other", "control-01"),
            ("api_provider", "python -m tests.fixtures.bob108_controlled_runtime --run-dir /cache/run", "another-run"),
        ):
            with self.subTest(role=role, command=command, run_id=run_id), patch.object(
                bob108_test_runtime, "_process_command", return_value=command
            ), patch.object(bob108_test_runtime.os, "killpg") as kill:
                with self.assertRaises(RuntimeError):
                    bob108_test_runtime._stop_controlled_process(
                        {"pid": 123, "run_id": run_id}, run_dir=Path("/cache/run"), role=role
                    )
                kill.assert_not_called()

    def controlled_manifest(self) -> dict:
        return {
            "run_id": "control-01",
            "ports": {"api": 44817, "provider": 44818, "ui": 3577},
            "runtime": {
                "browser_host": "127.0.0.1",
                "browser_port": 45815,
                "browser_user": "bob108",
                "browser_db": "bob108_browser",
            },
            "service_identities": {"browser": {"system_identifier": "202"}},
        }

    def test_controlled_environment_selects_owned_home_and_database(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary)
            with patch.dict("os.environ", {"HOME": "/ambient/home", "HEXIS_INSTANCE": "main", "POSTGRES_DB": "hexis_memory"}):
                child = bob108_test_runtime._controlled_environment(
                    self.controlled_manifest(), "owned-password", run_dir=run_dir
                )
            self.assertEqual(child["HOME"], str(run_dir / "runtime-home"))
            self.assertTrue(Path(child["HOME"]).is_dir())
            self.assertEqual(child["POSTGRES_HOST"], "127.0.0.1")
            self.assertEqual(child["POSTGRES_PORT"], "45815")
            self.assertEqual(child["POSTGRES_USER"], "bob108")
            self.assertEqual(child["POSTGRES_DB"], "bob108_browser")
            self.assertEqual(child["POSTGRES_PASSWORD"], "owned-password")
            self.assertNotIn("HEXIS_INSTANCE", child)
            result = subprocess.run(
                [sys.executable, "-c", "import json; from core.agent_api import db_dsn_from_env; from core.instance import InstanceRegistry; print(json.dumps({'dsn': db_dsn_from_env(), 'registry': str(InstanceRegistry.CONFIG_FILE)}))"],
                cwd=bob108_test_runtime.REPO_ROOT, env=child, capture_output=True, text=True, check=True,
            )
            actual = json.loads(result.stdout)
            self.assertEqual(actual["dsn"], "postgresql://bob108:owned-password@127.0.0.1:45815/bob108_browser")
            self.assertEqual(actual["registry"], str(run_dir / "runtime-home/.hexis/instances.json"))

    def test_controlled_home_refuses_existing_instance_registry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary)
            registry = run_dir / "runtime-home/.hexis/instances.json"
            registry.parent.mkdir(parents=True)
            registry.write_text('{"current": "main"}', encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "registry"):
                bob108_test_runtime._controlled_environment(
                    self.controlled_manifest(), "owned-password", run_dir=run_dir
                )

    def test_controlled_home_refuses_symlink_to_ambient_home(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary)
            (run_dir / "runtime-home").symlink_to(Path.home(), target_is_directory=True)
            with self.assertRaisesRegex(RuntimeError, "home"):
                bob108_test_runtime._controlled_environment(
                    self.controlled_manifest(), "owned-password", run_dir=run_dir
                )

    def test_controlled_ui_uses_webpack_with_reused_dependencies(self) -> None:
        ui_root = Path("/cache/run/ui-src")
        command = bob108_test_runtime._controlled_ui_command("/usr/bin/node", ui_root, 3577)
        self.assertEqual(command[:3], ["/usr/bin/node", str(ui_root / "node_modules/next/dist/bin/next"), "dev"])
        self.assertIn("--webpack", command)
        self.assertEqual(command[-4:], ["--hostname", "127.0.0.1", "--port", "3577"])

    def test_prepare_rejects_non_absolute_run_directory_before_side_effects(self) -> None:
        with patch.object(bob108_test_runtime, "socket") as socket_module, patch.object(
            bob108_test_runtime.subprocess, "run"
        ) as run:
            with self.assertRaisesRegex(ValueError, "absolute"):
                bob108_test_runtime.validate_run_dir("relative/run")

        socket_module.socket.assert_not_called()
        run.assert_not_called()

    def test_prepare_refuses_unexpected_env_file(self) -> None:
        with patch.object(bob108_test_runtime.Path, "exists", return_value=True):
            with self.assertRaisesRegex(ValueError, r"\.env"):
                bob108_test_runtime.validate_repo_environment(
                    repo_root=bob108_test_runtime.Path("/repo"),
                    run_dir=bob108_test_runtime.Path("/cache/run"),
                )

    def test_ports_must_be_available_before_compose_is_started(self) -> None:
        with patch.object(bob108_test_runtime, "port_is_available", return_value=False), patch.object(
            bob108_test_runtime.subprocess, "run"
        ) as run:
            with self.assertRaisesRegex(RuntimeError, "port"):
                bob108_test_runtime.require_available_ports([45815, 45816])

        run.assert_not_called()

    def test_pytest_environment_drops_ambient_database_identity(self) -> None:
        ambient = {
            "POSTGRES_HOST": "ambient-host",
            "POSTGRES_PORT": "9999",
            "POSTGRES_USER": "ambient-user",
            "POSTGRES_PASSWORD": "ambient-secret",
            "POSTGRES_DB": "ambient-db",
            "POSTGRES_ADMIN_DB": "ambient-admin",
            "HEXIS_DATABASE_URL": "postgres://ambient",
            "FAKE_EMBEDDINGS_PORT": "9998",
            "EMBEDDING_DIMENSION": "1024",
            "EMBEDDING_SERVICE_URL": "http://ambient-embeddings",
            "EMBEDDING_MODEL_ID": "ambient-model",
            "UNRELATED": "kept",
        }
        with patch.dict("os.environ", ambient, clear=True):
            child = bob108_test_runtime.build_pytest_environment(
                runtime_env={
                    "pytest_host": "127.0.0.1",
                    "pytest_port": 45816,
                    "pytest_user": "bob108",
                    "pytest_db": "bob108_pytest",
                    "pytest_admin_db": "postgres",
                    "embedding_port": 45435,
                    "embedding_dimension": 768,
                    "fake_embeddings_url": "http://127.0.0.1:45435/api/embed",
                    "embedding_model_id": "bob108-fake-768",
                },
                password="owned-password",
            )

        self.assertEqual(child["POSTGRES_HOST"], "127.0.0.1")
        self.assertEqual(child["POSTGRES_PORT"], "45816")
        self.assertEqual(child["POSTGRES_PASSWORD"], "owned-password")
        self.assertEqual(child["FAKE_EMBEDDINGS_PORT"], "45435")
        self.assertEqual(child["EMBEDDING_DIMENSION"], "768")
        self.assertEqual(child["EMBEDDING_SERVICE_URL"], "http://127.0.0.1:45435/api/embed")
        self.assertEqual(child["EMBEDDING_MODEL_ID"], "bob108-fake-768")
        self.assertNotIn("HEXIS_DATABASE_URL", child)
        self.assertNotIn("UNRELATED", child)

    def test_compose_projects_have_distinct_volume_names(self) -> None:
        names = bob108_test_runtime.control_volume_names("hexis-bob108-control")
        self.assertEqual(len(names), 2)
        self.assertNotEqual(names["pytest"], names["browser"])


if __name__ == "__main__":
    unittest.main()
