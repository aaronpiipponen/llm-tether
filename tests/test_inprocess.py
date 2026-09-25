"""In-process tests for logs and reconcile that must not reach the network.

These import the package with the temporary workspace overrides already in the environment, then
monkeypatch ``transport.run_remote`` so no SSH is attempted.
"""

from __future__ import annotations

import argparse
import dataclasses
import importlib
import io
import os
import subprocess
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent.parent))

from support import MODEL_LARGE, MODEL_SMALL, Workspace, load_package, session  # noqa: E402


class InProcessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.ws = Workspace()
        os.environ.update(cls.ws.env)
        cls.cli, cls.state, cls.transport, connection_cls = load_package()
        cls.connection = connection_cls("test-worker", "TestDistro", "/srv/worker", 5, 30, 30)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.ws.cleanup()

    def setUp(self) -> None:
        self.ws.write_state([])
        self.addCleanup(setattr, self.transport, "run_remote", self.transport.run_remote)

    def _capture(self, func, *args) -> str:
        out = io.StringIO()
        with redirect_stdout(out):
            func(*args)
        return out.getvalue()

    def _submodule(self, suffix: str):
        package = self.cli.__name__.rsplit(".", 1)[0]
        return importlib.import_module(f"{package}.{suffix}")

    def _model(self, key: str):
        return self._submodule("catalog").model_by_key(key)

    def test_launch_script_omits_ngl_when_auto(self) -> None:
        lifecycle = self._submodule("lifecycle")
        model = self._model(MODEL_SMALL)
        script = lifecycle.launch_script(self.connection, model, 1, 19100, 1024, "off")
        self.assertNotIn("--n-gpu-layers", script)

    def test_launch_script_passes_explicit_ngl(self) -> None:
        lifecycle = self._submodule("lifecycle")
        model = dataclasses.replace(self._model(MODEL_SMALL), gpu_layers="all")
        script = lifecycle.launch_script(self.connection, model, 1, 19100, 1024, "off")
        self.assertIn("--n-gpu-layers all", script)

    def test_config_sees_explicit_gpu_layers(self) -> None:
        self.assertEqual(self._model(MODEL_LARGE).gpu_layers, "8")

    def test_provider_entry_uses_native_v2_schema(self) -> None:
        opencode = self._submodule("opencode")
        model = dataclasses.replace(self._model(MODEL_SMALL), supports_reasoning=True)
        entry = opencode.opencode_entry(model, 1, 19100, 4096, "off")
        self.assertEqual(entry["package"], "aisdk:@ai-sdk/openai-compatible")
        self.assertEqual(entry["settings"]["baseURL"], "http://127.0.0.1:19100/v1")
        self.assertNotIn("npm", entry)
        self.assertNotIn("options", entry)
        spec = entry["models"][model.alias]
        self.assertEqual(spec["capabilities"]["tools"], model.tool_call)
        self.assertEqual(spec["limit"]["context"], 4096)
        self.assertEqual(spec["settings"], {"reasoningEffort": "none"})

    def test_add_and_remove_provider_use_providers_key(self) -> None:
        opencode = self._submodule("opencode")
        self.ws.write_opencode({"providers": {"nvidia": {}}})
        with redirect_stdout(io.StringIO()):
            provider = opencode.add_provider(self._model(MODEL_SMALL), 1, 19100, 1024, "off")
        document = self.ws.read_opencode()
        self.assertNotIn("provider", document)
        self.assertEqual(set(document["providers"]), {"nvidia", provider})
        with redirect_stdout(io.StringIO()):
            opencode.remove_provider(provider)
        self.assertEqual(set(self.ws.read_opencode()["providers"]), {"nvidia"})

    def test_failed_start_stops_remote_server_and_releases_wsl(self) -> None:
        lifecycle = self._submodule("lifecycle")
        model = self._model(MODEL_SMALL)
        scripts: list[str] = []
        terminated: list[bool] = []

        def fake_remote(_connection, script):
            scripts.append(script)
            return subprocess.CompletedProcess([], 0, stdout=b"", stderr=b"")

        def fail_healthy(*_args, **_kwargs):
            raise SystemExit("never healthy")

        for name, value in (
            ("warn_if_vram_short", lifecycle.warn_if_vram_short),
            ("add_provider", lifecycle.add_provider),
            ("remove_provider", lifecycle.remove_provider),
            ("wait_healthy", lifecycle.wait_healthy),
        ):
            self.addCleanup(setattr, lifecycle, name, value)
        self.addCleanup(setattr, self.transport, "start_keeper", self.transport.start_keeper)
        self.addCleanup(setattr, self.transport, "terminate_wsl", self.transport.terminate_wsl)

        lifecycle.warn_if_vram_short = lambda *_args, **_kwargs: None
        lifecycle.add_provider = lambda *_args, **_kwargs: f"local-{MODEL_SMALL}"
        lifecycle.remove_provider = lambda _provider: None
        lifecycle.wait_healthy = fail_healthy
        self.transport.run_remote = fake_remote
        self.transport.start_keeper = lambda *_args, **_kwargs: 999999
        self.transport.terminate_wsl = lambda *_args, **_kwargs: terminated.append(True)

        with self.assertRaises(SystemExit):
            lifecycle.start_model(self.connection, model, 1024, "off")

        self.assertTrue(
            any("kill" in script and f"{MODEL_SMALL}-1.pid" in script for script in scripts)
        )
        self.assertEqual(terminated, [True])

    def test_logs_prints_remote_and_keeper(self) -> None:
        self.state.STATE_DIR.mkdir(parents=True, exist_ok=True)
        (self.state.STATE_DIR / f"{MODEL_SMALL}-1.keeper.log").write_text(
            "keeper line\n", encoding="utf-8"
        )
        args = argparse.Namespace(model=MODEL_SMALL, instance=1, lines=50, provider=None)

        def fake_remote(_connection, _script):
            import subprocess

            return subprocess.CompletedProcess(
                [], 0, stdout=b"remote line one\nremote line two\n", stderr=b""
            )

        self.transport.run_remote = fake_remote
        text = self._capture(self.cli.logs_command, self.connection, args)
        self.assertIn("remote line one", text)
        self.assertIn("keeper line", text)
        self.assertNotIn("remote log not found", text)

    def test_logs_reports_missing_remote(self) -> None:
        import subprocess

        args = argparse.Namespace(model=MODEL_LARGE, instance=1, lines=50, provider=None)
        self.transport.run_remote = lambda _c, _s: subprocess.CompletedProcess(
            [], 0, stdout=b"__MISSING__\n", stderr=b""
        )
        text = self._capture(self.cli.logs_command, self.connection, args)
        self.assertIn("remote log not found", text)

    def test_reconcile_dry_run_drops_stale_session_without_ssh(self) -> None:
        dead = session(f"local-{MODEL_SMALL}-2", MODEL_SMALL, 2, [])
        dead["keeper_pid"] = 999999
        self.ws.write_state([dead])

        def explode(_connection, _script):
            raise AssertionError("reconcile dry-run must not run remote commands")

        self.transport.run_remote = explode
        text = self._capture(self.cli.reconcile_command, self.connection, False)
        self.assertIn("drop stale session local-small-2", text)
        self.assertIn("dry-run", text)

    def test_stop_selects_by_provider(self) -> None:
        active = session(f"local-{MODEL_SMALL}", MODEL_SMALL, 1, [])
        recorded = []
        original = self.cli.stop_session
        self.addCleanup(setattr, self.cli, "stop_session", original)
        self.cli.stop_session = lambda _connection, item: recorded.append(item)
        args = argparse.Namespace(
            all=False, provider=f"local-{MODEL_SMALL}", model=None, instance=None
        )
        self.cli._stop_selected(self.connection, [active], args)
        self.assertEqual(recorded, [active])

    def test_stop_by_unknown_provider_fails(self) -> None:
        args = argparse.Namespace(all=False, provider="local-missing", model=None, instance=None)
        with self.assertRaises(SystemExit):
            self.cli._stop_selected(self.connection, [], args)

    def test_clean_removes_stale_local_and_reports_remote(self) -> None:
        self.state.STATE_DIR.mkdir(parents=True, exist_ok=True)
        stale = self.state.STATE_DIR / f"{MODEL_SMALL}-1.keeper.log"
        stale.write_text("stale\n", encoding="utf-8")
        self.ws.write_state([session(f"local-{MODEL_SMALL}-2", MODEL_SMALL, 2, [])])
        seen = {}

        def fake_remote(_connection, script):
            seen["script"] = script
            return subprocess.CompletedProcess(
                [], 0, stdout=b"removed remote small-1.log\n", stderr=b""
            )

        self.transport.run_remote = fake_remote
        text = self._capture(self.cli.clean_command, self.connection, True)
        self.assertFalse(stale.exists())
        self.assertIn("removed local", text)
        self.assertIn("removed remote small-1.log", text)
        self.assertIn("grep -Fxq -e small-2", seen["script"])


if __name__ == "__main__":
    unittest.main()
