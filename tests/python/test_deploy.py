import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from unittest.mock import patch
from pathlib import Path

from vccp_runtime.deploy import _macos_dispatcher_label, deploy_host, main
from vccp_runtime.host_deployment import _runtime_config_digest

SHA = "5e9688e1716d570431692ae1b0d61c5c44a2eee4"


class DeployHostTests(unittest.TestCase):
    def test_default_macos_label_includes_owner_and_has_collision_resistant_suffix(self):
        first = _macos_dispatcher_label("owner-one/app")
        second = _macos_dispatcher_label("owner-two/app")
        self.assertNotEqual(first, second)
        self.assertEqual(first, _macos_dispatcher_label("owner-one/app"))

    def test_upgrade_target_does_not_change_runtime_config_digest(self):
        with tempfile.TemporaryDirectory() as folder:
            config = Path(folder) / "runtime.json"
            config.write_text('{"target_repository":"owner/repo","upgrade_target":{"revision":"abc"}}', encoding="utf-8")
            before = _runtime_config_digest(config)
            config.write_text('{"target_repository":"owner/repo"}', encoding="utf-8")
            self.assertEqual(_runtime_config_digest(config), before)

    def test_deploy_prepares_exact_revision_before_host_reconcile(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "source"
            source.mkdir()
            import subprocess
            subprocess.run(["git", "init", "-q", str(source)], check=True)
            subprocess.run(["git", "-C", str(source), "config", "user.email", "test@example.com"], check=True)
            subprocess.run(["git", "-C", str(source), "config", "user.name", "Test"], check=True)
            (source / "runtime.py").write_text("runtime\n")
            subprocess.run(["git", "-C", str(source), "add", "."], check=True)
            subprocess.run(["git", "-C", str(source), "commit", "-qm", "runtime"], check=True)
            revision = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
            events = []
            config = {"database_path": str(root / "state.sqlite"), "workspace": str(source), "owner_id": "owner",
                      "dispatcher": {"label": "com.vccp.test-dispatcher", "plist_path": str(root / "agent.plist")}}
            with patch("vccp_runtime.deploy.os.getuid", return_value=501, create=True):
                result = deploy_host(repo="owner/repo", manifest={"vccp": {"revision": revision}},
                    local_config=config, manifest_path=str(root / "manifest.yml"), runtime_config_path=str(root / "runtime.json"),
                    source_root=str(source), runtimes_root=str(root / "runtimes"), platform="darwin",
                    readiness=lambda runtime: {"status": "ready"}, host_reconciler=lambda **kwargs: events.append(kwargs) or {"status": "running"})
            self.assertEqual(result["status"], "running")
            self.assertEqual(events[0]["runtime_root"], str((root / "runtimes" / revision).resolve()))
            self.assertEqual(result["revision"], revision)

    def test_old_controller_hands_deployment_to_the_exact_pinned_runtime(self):
        import subprocess
        import json
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder)
            subprocess.run(["git", "init", "-q", str(source)], check=True)
            subprocess.run(["git", "-C", str(source), "config", "user.email", "test@example.com"], check=True)
            subprocess.run(["git", "-C", str(source), "config", "user.name", "Test"], check=True)
            (source / "runtime.py").write_text("controller A\n")
            subprocess.run(["git", "-C", str(source), "add", "."], check=True)
            subprocess.run(["git", "-C", str(source), "commit", "-qm", "controller A"], check=True)
            pinned_revision = "b" * 40
            calls = []
            def runner(command, **kwargs):
                calls.append((command, kwargs))
                return SimpleNamespace(returncode=0, stdout=json.dumps({"status": "running"}))
            with patch("vccp_runtime.deploy.ensure_runtime_checkout", return_value=source):
                result = deploy_host(repo="owner/repo", manifest={"vccp": {"revision": pinned_revision}},
                    local_config={}, manifest_path=str(source / "manifest.yml"),
                    runtime_config_path=str(source / "runtime.json"), source_root=str(source),
                    runtimes_root=str(source / "runtimes"), runner=runner)
            self.assertEqual(result["status"], "running")
            self.assertEqual(calls[0][0][1:], ["-m", "vccp_runtime.deploy"])
            self.assertEqual(calls[0][1]["cwd"], str(source))


class ProviderRebindCheckTests(unittest.TestCase):
    """The onboarding CLI switches Providers only through this deploy entry point."""

    def run_check(self, database):
        payload = {"operation": "provider-rebind-check", "repo": "owner/repo",
                   "local_config": {"database_path": str(database)}}
        out = io.StringIO()
        with patch("sys.stdin", io.StringIO(json.dumps(payload))), patch("sys.stdout", out):
            code = main()
        return code, json.loads(out.getvalue())

    def test_provider_rebind_check_is_ready_without_active_work(self):
        with tempfile.TemporaryDirectory() as folder:
            database = Path(folder) / "state.sqlite"
            with closing(sqlite3.connect(database)) as connection:
                connection.execute("CREATE TABLE attempts(repo TEXT, phase TEXT, closeout_pending INTEGER)")
                connection.execute("INSERT INTO attempts VALUES('owner/repo', 'MERGED_SUCCESS', 0)")
                connection.commit()
            code, result = self.run_check(database)
        self.assertEqual((code, result), (2, {"status": "ready", "reason": None}))

    def test_provider_rebind_check_blocks_on_active_work(self):
        with tempfile.TemporaryDirectory() as folder:
            database = Path(folder) / "state.sqlite"
            with closing(sqlite3.connect(database)) as connection:
                connection.execute("CREATE TABLE attempts(repo TEXT, phase TEXT, closeout_pending INTEGER)")
                connection.execute("INSERT INTO attempts VALUES('owner/repo', 'PR_BOUND', 0)")
                connection.commit()
            code, result = self.run_check(database)
        self.assertEqual(code, 2)
        self.assertEqual(result["status"], "blocked")
        self.assertIn("active Consumer execution", result["reason"])

    def test_provider_rebind_check_never_raises_name_error(self):
        with tempfile.TemporaryDirectory() as folder:
            database = Path(folder) / "missing" / "state.sqlite"
            code, result = self.run_check(database)
        self.assertEqual((code, result), (2, {"status": "ready", "reason": None}))

    def pushes(self, database, rows):
        with closing(sqlite3.connect(database)) as connection:
            connection.execute("CREATE TABLE attempts(repo TEXT, phase TEXT, closeout_pending INTEGER)")
            connection.execute("CREATE TABLE controlled_pushes(attempt_id TEXT, repo TEXT, "
                               "destination_ref TEXT, expected_sha TEXT, phase TEXT)")
            connection.executemany("INSERT INTO controlled_pushes VALUES(?,?,?,?,?)", rows)
            connection.commit()

    def test_intent_superseded_by_the_same_completed_push_does_not_block(self):
        with tempfile.TemporaryDirectory() as folder:
            database = Path(folder) / "state.sqlite"
            self.pushes(database, [("attempt-1", "owner/repo", "branch/1", SHA, "intent"),
                                   ("attempt-1", "owner/repo", "branch/1", SHA, "completed")])
            code, result = self.run_check(database)
        self.assertEqual((code, result), (2, {"status": "ready", "reason": None}))

    def test_a_lone_intent_still_blocks(self):
        with tempfile.TemporaryDirectory() as folder:
            database = Path(folder) / "state.sqlite"
            self.pushes(database, [("attempt-1", "owner/repo", "branch/1", SHA, "intent")])
            code, result = self.run_check(database)
        self.assertEqual(code, 2)
        self.assertEqual(result["status"], "blocked")
        self.assertIn("active controlled push", result["reason"])

    def test_completed_push_must_match_attempt_ref_and_sha_to_supersede(self):
        other = "a" * 40
        for label, completed in {
            "another attempt": ("attempt-2", "owner/repo", "branch/1", SHA, "completed"),
            "another ref": ("attempt-1", "owner/repo", "branch/2", SHA, "completed"),
            "another sha": ("attempt-1", "owner/repo", "branch/1", other, "completed"),
            "another repository": ("attempt-1", "owner/other", "branch/1", SHA, "completed"),
        }.items():
            with self.subTest(label), tempfile.TemporaryDirectory() as folder:
                database = Path(folder) / "state.sqlite"
                self.pushes(database, [("attempt-1", "owner/repo", "branch/1", SHA, "intent"), completed])
                code, result = self.run_check(database)
                self.assertEqual(code, 2)
                self.assertEqual(result["status"], "blocked")


if __name__ == "__main__":
    unittest.main()