"""CLI-level offline tests: run the real package against a throwaway workspace."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import MODEL_LARGE, MODEL_SMALL, Workspace, session  # noqa: E402


class OfflineCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ws = Workspace()
        self.addCleanup(self.ws.cleanup)

    def test_doctor_is_clean_on_empty_state(self) -> None:
        self.ws.write_state([])
        result = self.ws.run("doctor", "--json")
        payload = json.loads(result.stdout)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(payload["problems"], 0)
        self.assertIn("config", payload["paths"])
        plugin = [c for c in payload["checks"] if c["name"] == "reload plugin"]
        self.assertEqual(plugin[0]["status"], "warn")

    def test_models_lists_catalog(self) -> None:
        result = self.ws.run("models", "--json")
        payload = json.loads(result.stdout)
        keys = [model["key"] for model in payload["models"]]
        self.assertEqual(keys, [MODEL_SMALL, MODEL_LARGE])
        self.assertIn("estimated_vram_mib", payload["models"][0])

    def test_where_reports_resolved_paths(self) -> None:
        result = self.ws.run("where", "--json")
        payload = json.loads(result.stdout)
        self.assertEqual(payload["config"], str(self.ws.config))
        self.assertEqual(payload["agents"], str(self.ws.agents))
        self.assertEqual(payload["state"], str(self.ws.state_dir))

    def test_start_dry_run_does_not_start(self) -> None:
        self.ws.write_state([])
        result = self.ws.run(
            "start",
            "--model",
            MODEL_SMALL,
            "--context",
            "1024",
            "--reasoning",
            "off",
            "--count",
            "2",
            "--dry-run",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Dry run", result.stdout)
        self.assertIn(f"provider local-{MODEL_SMALL},", result.stdout)
        self.assertIn(f"local-{MODEL_SMALL}-2", result.stdout)
        self.assertEqual(self.ws.read_state(), [])

    def test_status_reports_one_session(self) -> None:
        stamp = self.ws.agents / f"example-reviewer-{MODEL_SMALL}-1.md"
        stamp.write_text("stub", encoding="utf-8")
        self.ws.write_state([session(f"local-{MODEL_SMALL}", MODEL_SMALL, 1, [str(stamp)])])
        result = self.ws.run("status", "--json")
        payload = json.loads(result.stdout)
        one = payload["sessions"][0]
        self.assertEqual(one["provider"], f"local-{MODEL_SMALL}")
        self.assertEqual(one["subagents"], [stamp.name])
        self.assertIsInstance(one["uptime_seconds"], int)
        self.assertTrue(one["health"].startswith("down"))

    def test_doctor_flags_health_and_orphans(self) -> None:
        stamp = self.ws.agents / f"example-reviewer-{MODEL_SMALL}-1.md"
        stamp.write_text("stub", encoding="utf-8")
        orphan = self.ws.agents / f"example-reviewer-{MODEL_LARGE}-1.md"
        orphan.write_text("stub", encoding="utf-8")
        self.ws.write_opencode(
            {"provider": {"nvidia": {}, f"local-{MODEL_SMALL}": {}, "local-ghost": {}}}
        )
        self.ws.write_state([session(f"local-{MODEL_SMALL}", MODEL_SMALL, 1, [str(stamp)])])
        result = self.ws.run("doctor", "--json")
        payload = json.loads(result.stdout)
        names = {check["name"]: check for check in payload["checks"]}
        self.assertEqual(result.returncode, 1)
        self.assertEqual(names[f"state[local-{MODEL_SMALL}]"]["status"], "ok")
        self.assertTrue(any(name.startswith("orphan[example-reviewer-large") for name in names))

    def test_subagents_lists_recorded_and_orphans(self) -> None:
        stamp = self.ws.agents / f"example-reviewer-{MODEL_SMALL}-1.md"
        stamp.write_text("stub", encoding="utf-8")
        orphan = self.ws.agents / f"example-reviewer-{MODEL_LARGE}-1.md"
        orphan.write_text("stub", encoding="utf-8")
        self.ws.write_state([session(f"local-{MODEL_SMALL}", MODEL_SMALL, 1, [str(stamp)])])
        result = self.ws.run("subagents")
        self.assertIn(stamp.name, result.stdout)
        self.assertIn(orphan.name, result.stdout)

    def test_create_subagent_dry_run_writes_nothing(self) -> None:
        self.ws.write_state([session(f"local-{MODEL_SMALL}", MODEL_SMALL, 1, [])])
        before_state = self.ws.read_state()
        before_files = sorted(path.name for path in self.ws.agents.iterdir())
        result = self.ws.run(
            "create-subagent",
            "--provider",
            f"local-{MODEL_SMALL}",
            "--template",
            "example-reviewer",
            "--dry-run",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("would stamp", result.stdout)
        self.assertEqual(self.ws.read_state(), before_state)
        self.assertEqual(sorted(path.name for path in self.ws.agents.iterdir()), before_files)

    def test_create_all_then_remove(self) -> None:
        self.ws.write_state([session(f"local-{MODEL_SMALL}", MODEL_SMALL, 1, [])])
        result = self.ws.run("create-subagent", "--provider", f"local-{MODEL_SMALL}", "--all")
        self.assertEqual(result.returncode, 0, result.stderr)
        stamped = self.ws.agents / f"example-reviewer-{MODEL_SMALL}-1.md"
        self.assertTrue(stamped.is_file())
        self.assertIn(str(stamped), [str(p) for p in self.ws.read_state()[0]["agents"]])

        result = self.ws.run(
            "remove-subagent",
            "--provider",
            f"local-{MODEL_SMALL}",
            "--template",
            "example-reviewer",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(stamped.exists())
        self.assertEqual(self.ws.read_state()[0]["agents"], [])

    def test_remove_subagent_fails_for_unrecorded_file(self) -> None:
        self.ws.write_state([session(f"local-{MODEL_SMALL}", MODEL_SMALL, 1, [])])
        result = self.ws.run(
            "remove-subagent", "--provider", f"local-{MODEL_SMALL}", "--file", "/nope.md"
        )
        self.assertNotEqual(result.returncode, 0)

    def test_prune_defaults_to_reporting(self) -> None:
        orphan = self.ws.agents / f"example-reviewer-{MODEL_LARGE}-1.md"
        orphan.write_text("stub", encoding="utf-8")
        self.ws.write_state([])
        result = self.ws.run("prune-subagents")
        self.assertIn(orphan.name, result.stdout)
        self.assertIn("Would remove", result.stdout)
        self.assertTrue(orphan.exists())

        result = self.ws.run("prune-subagents", "--execute")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(orphan.exists())

    def test_reconcile_defaults_to_reporting(self) -> None:
        # Keep one live session so reconcile never reaches its final WSL-terminate step (which
        # would shell out over SSH); this test stays local.
        self.ws.write_state([session(f"local-{MODEL_SMALL}", MODEL_SMALL, 1, [])])
        self.ws.write_opencode(
            {"provider": {"nvidia": {}, f"local-{MODEL_SMALL}": {}, "local-ghost": {}}}
        )
        result = self.ws.run("reconcile")
        self.assertIn("remove provider local-ghost", result.stdout)
        self.assertIn("dry-run", result.stdout)
        self.assertIn("local-ghost", self.ws.read_opencode()["provider"])

        result = self.ws.run("reconcile", "--execute")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("local-ghost", self.ws.read_opencode()["provider"])

    def test_reconcile_restores_missing_stamp(self) -> None:
        stamp = self.ws.agents / f"example-reviewer-{MODEL_SMALL}-1.md"
        self.assertFalse(stamp.exists())
        self.ws.write_opencode({"provider": {"nvidia": {}, f"local-{MODEL_SMALL}": {}}})
        self.ws.write_state([session(f"local-{MODEL_SMALL}", MODEL_SMALL, 1, [str(stamp)])])
        result = self.ws.run("reconcile", "--execute")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(stamp.is_file())
        self.assertIn("restore stamp", result.stdout)

    def test_remove_subagent_by_relative_file(self) -> None:
        self.ws.write_state([session(f"local-{MODEL_SMALL}", MODEL_SMALL, 1, [])])
        created = self.ws.run("create-subagent", "--provider", f"local-{MODEL_SMALL}", "--all")
        self.assertEqual(created.returncode, 0, created.stderr)
        stamp = self.ws.agents / f"example-reviewer-{MODEL_SMALL}-1.md"
        self.assertTrue(stamp.is_file())
        result = self.ws.run(
            "remove-subagent", "--provider", f"local-{MODEL_SMALL}", "--file", stamp.name
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(stamp.exists())

    def test_start_count_must_be_positive(self) -> None:
        result = self.ws.run("start", "--count", "0", "--model", MODEL_SMALL)
        self.assertNotEqual(result.returncode, 0)

    def test_invalid_gpu_layers_is_rejected(self) -> None:
        text = self.ws.config.read_text(encoding="utf-8")
        text = text.replace('key = "small"', 'key = "small"\ngpu_layers = "banana"')
        self.ws.config.write_text(text, encoding="utf-8")
        result = self.ws.run("doctor", "--json")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("gpu_layers", result.stderr)


if __name__ == "__main__":
    unittest.main()
