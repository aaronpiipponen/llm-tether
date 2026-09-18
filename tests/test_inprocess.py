"""In-process tests for logs and reconcile that must not reach the network.

These import the package with the temporary workspace overrides already in the environment, then
monkeypatch ``transport.run_remote`` so no SSH is attempted.
"""

from __future__ import annotations

import argparse
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
