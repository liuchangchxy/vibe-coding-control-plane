import json
import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO

from vccp_runtime import runner
from vccp_runtime.core import RuntimeConfig
from vccp_runtime.runner import RuntimeAlreadyRunning, RuntimeLock, run_continuous


def _checkout_head():
    source_root = Path(runner.__file__).resolve().parent.parent
    return subprocess.run(["git", "rev-parse", "--verify", "HEAD"], cwd=source_root,
                          check=True, capture_output=True, text=True).stdout.strip()


class RuntimeRunnerTests(unittest.TestCase):
    def test_process_lock_excludes_second_instance_and_releases(self):
        with tempfile.TemporaryDirectory() as folder:
            path = str(Path(folder) / "runtime.db")
            first = RuntimeLock("owner/repo", path).acquire()
            try:
                with self.assertRaises(RuntimeAlreadyRunning):
                    RuntimeLock("OWNER/REPO", path).acquire()
            finally:
                first.release()
            second = RuntimeLock("owner/repo", path).acquire()
            second.release()

    def test_continuous_loop_uses_injected_sleep_and_stop(self):
        class Fake:
            def __init__(self):
                self.cycles = 0
                self.core = self
                self.workflow = self
                self.lifecycle = self

            def reconcile_once(self, *args):
                return {"status": "ok", "items": []}

            def observe_implementer_progress(self, *args):
                return {"status": "observed", "items": []}

            def advance_once(self, *args):
                self.cycles += 1
                return {"status": "ok", "items": []}

            def discover_ready(self, repo):
                return []

            def dispatch_initial(self, *args):
                raise AssertionError("no ready Issue")

        runtime = Fake()
        sleeps = []
        observed = []
        result = run_continuous(runtime, "owner/repo", "owner", poll_interval=5,
                                sleep=sleeps.append, stop=lambda: runtime.cycles >= 4,
                                on_cycle=lambda item: observed.append(item["dispatches"]))
        self.assertIsNone(result)
        self.assertEqual(observed, [[], []])
        self.assertEqual(sleeps, [5])

    def test_continuous_cli_reports_discovery_failure_without_exiting(self):
        manifest = {"vccp": {"revision": _checkout_head()}}
        local = {"database_path": "state.db", "owner_id": "owner", "workspace": ".",
                 "target_repository": "owner/repo", "enrollment": {"authorized_repository": "owner/repo"}}
        failure = {"ready_discovery": {"status": "discovery_unavailable",
                                        "detail": "GitHub API temporarily unavailable"}}

        class Lock:
            def __init__(self, *_): pass
            def __enter__(self): return self
            def __exit__(self, *_): pass

        def continuous(runtime, repo, owner_id, poll_interval, on_cycle=None):
            self.assertEqual((repo, owner_id, poll_interval), ("owner/repo", "owner", 30.0))
            on_cycle(failure)

        stderr = StringIO()
        with patch.object(runner, "_load", side_effect=[manifest, local]), \
             patch.object(runner, "_runtime_source_revision", return_value=_checkout_head()), \
             patch.object(runner, "_workspace_repository", return_value="owner/repo"), \
             patch.object(runner, "RuntimeLock", Lock), \
             patch.object(runner, "build_runtime", return_value=object()), \
             patch.object(runner, "run_continuous", side_effect=continuous), \
             redirect_stderr(stderr):
            runner.main(["--manifest", "manifest.yml", "--runtime-config", "runtime.json",
                         "--repo", "owner/repo", "--continuous"])

        event = json.loads(stderr.getvalue())
        self.assertEqual(event["event"], "ready_discovery_unavailable")
        self.assertEqual(event["repo"], "owner/repo")
        self.assertEqual(event["detail"], "GitHub API temporarily unavailable")

    def test_continuous_loop_reports_failure_then_recovers_on_next_cycle(self):
        class Runtime:
            def __init__(self):
                self.discovery_calls = 0
                self.workflow = self
                self.core = self
                self.lifecycle = self

            def advance_once(self, *_): return {"status": "ok", "items": []}
            def reconcile_once(self, *_): return {"status": "reconciled", "items": []}
            def observe_implementer_progress(self, *_): return {"status": "observed", "items": []}
            def discover_ready(self, _repo):
                self.discovery_calls += 1
                if self.discovery_calls == 1:
                    raise RuntimeError("temporary GitHub read failure")
                return []

        runtime = Runtime()
        stderr = StringIO()
        cycles = []
        sleeps = []
        run_continuous(
            runtime, "owner/repo", "owner", poll_interval=5, sleep=sleeps.append,
            stop=lambda: runtime.discovery_calls >= 2,
            on_cycle=lambda result: (cycles.append(result), runner._report_cycle(result, "owner/repo", stderr)),
        )

        self.assertEqual(len(cycles), 2)
        self.assertEqual(cycles[0]["ready_discovery"]["status"], "discovery_unavailable")
        self.assertEqual(cycles[1]["ready_discovery"], {"status": "discovered", "count": 0})
        self.assertEqual(sleeps, [5])
        events = [json.loads(line) for line in stderr.getvalue().splitlines()]
        self.assertEqual([event["event"] for event in events], ["ready_discovery_unavailable"])

    def test_cycle_reporter_is_quiet_for_normal_idle_cycle(self):
        stderr = StringIO()
        runner._report_cycle({
            "ready_discovery": {"status": "discovered", "count": 0},
            "dispatches": [],
            "recovery": {"status": "reconciled", "items": []},
            "progress": {"status": "observed", "items": []},
            "lifecycle": {"status": "ok", "items": []},
            "lifecycle_after_dispatch": {"status": "ok", "items": []},
        }, "owner/repo", stderr)
        self.assertEqual(stderr.getvalue(), "")

    def test_cycle_reporter_redacts_github_token_and_private_key_material(self):
        token = "ghp_" + "A" * 36
        private_key = "-----BEGIN PRIVATE KEY-----\nprivate credential bytes\n-----END PRIVATE KEY-----"
        stderr = StringIO()
        runner._report_cycle({"ready_discovery": {
            "status": "discovery_unavailable", "detail": f"{token} {private_key}"}},
            "owner/repo", stderr)
        output = stderr.getvalue()
        self.assertNotIn(token, output)
        self.assertNotIn(private_key, output)
        self.assertIn("[REDACTED]", output)

    def test_runtime_source_revision_rejects_dirty_checkout(self):
        head = type("Result", (), {"stdout": "a" * 40 + "\n"})()
        dirty = type("Result", (), {"stdout": " M vccp_runtime/runner.py\n"})()
        with patch.object(runner.subprocess, "run", side_effect=[head, dirty]):
            with self.assertRaisesRegex(ValueError, "uncommitted changes"):
                runner._runtime_source_revision()

    def test_cli_rejects_dirty_checkout_before_runtime_construction(self):
        manifest = {"vccp": {"revision": "a" * 40}}
        local = {"database_path": "state.db", "owner_id": "owner", "workspace": ".",
                 "target_repository": "owner/repo", "enrollment": {"authorized_repository": "owner/repo"}}
        with patch.object(runner, "_load", side_effect=[manifest, local]), \
             patch.object(runner, "_runtime_source_revision", side_effect=ValueError("running VCCP checkout has uncommitted changes")), \
             patch.object(runner, "build_runtime", side_effect=AssertionError("must fail before build")):
            with self.assertRaises(SystemExit) as error:
                runner.main(["--manifest", "manifest.yml", "--runtime-config", "runtime.json",
                             "--repo", "owner/repo", "--one-cycle"])
        self.assertEqual(error.exception.code, 2)

    def test_cli_rejects_manifest_revision_mismatch_before_runtime_construction(self):
        manifest = {"vccp": {"revision": "a" * 40}}
        local = {"database_path": "state.db", "owner_id": "owner", "workspace": ".",
                 "target_repository": "owner/repo", "enrollment": {"authorized_repository": "owner/repo"}}
        with patch.object(runner, "_load", side_effect=[manifest, local]), \
             patch.object(runner, "_workspace_repository", return_value="owner/repo"), \
             patch.object(runner, "_runtime_source_revision", return_value="b" * 40), \
             patch.object(runner, "build_runtime", side_effect=AssertionError("must fail before build")):
            with self.assertRaises(SystemExit) as error:
                runner.main(["--manifest", "manifest.yml", "--runtime-config", "runtime.json",
                             "--repo", "owner/repo", "--one-cycle"])
        self.assertEqual(error.exception.code, 2)

    def test_cli_acquires_lock_before_building_runtime(self):
        events = []

        class Lock:
            def __init__(self, repo, database):
                events.append(("identity", repo, database))
            def __enter__(self):
                events.append("lock")
                return self
            def __exit__(self, *_):
                events.append("release")

        runtime = object()
        def build(*_):
            events.append("build")
            return runtime
        def cycle(*_):
            events.append("cycle")
            return {"status": "ok"}

        local = {"database_path": "state.db", "owner_id": "owner", "workspace": ".",
                 "target_repository": "owner/repo", "enrollment": {"authorized_repository": "owner/repo"}}
        manifest = {"vccp": {"revision": _checkout_head()}}
        with patch.object(runner, "_load", side_effect=[manifest, local]), \
             patch.object(runner, "_runtime_source_revision", return_value=_checkout_head()), \
             patch.object(runner, "_workspace_repository", return_value="owner/repo"), \
             patch.object(runner, "RuntimeLock", Lock), \
             patch.object(runner, "build_runtime", side_effect=build), \
             patch.object(runner, "run_cycle", side_effect=cycle), redirect_stdout(StringIO()):
            runner.main(["--manifest", "manifest.json", "--runtime-config", "runtime.json",
                         "--repo", "owner/repo", "--one-cycle"])
        self.assertEqual(events, [("identity", "owner/repo", "state.db"), "lock", "build", "cycle", "release"])

    def test_reviewer_retry_cli_requires_human_authorization_for_exact_head(self):
        with patch.object(runner, "_load", side_effect=AssertionError("must reject before loading")), \
                patch.object(runner, "build_runtime", side_effect=AssertionError("must not build runtime")):
            with self.assertRaises(SystemExit) as error:
                runner.main(["--manifest", "manifest.yml", "--runtime-config", "runtime.json",
                             "--repo", "owner/repo", "--retry-reviewer", "--issue", "7",
                             "--pr", "12", "--head-sha", "a" * 40,
                             "--authorize-reviewer-retry-for", "b" * 40])
        self.assertEqual(error.exception.code, 2)

    def test_reviewer_retry_cli_invokes_local_driver_under_runtime_lock(self):
        head = "a" * 40
        events = []
        class Lock:
            def __init__(self, repo, database): events.append(("identity", repo, database))
            def __enter__(self): events.append("lock"); return self
            def __exit__(self, *_): events.append("release")
        class Reviewer:
            def retry_failed(self, repo, issue, pr, current_head, authorization_head):
                events.append(("retry", repo, issue, pr, current_head, authorization_head))
                return {"status": "reviewer_running", "execution_id": "claude-session", "head_sha": current_head}
        local = {"database_path": "state.db", "owner_id": "owner", "workspace": ".",
                 "target_repository": "owner/repo", "enrollment": {"authorized_repository": "owner/repo"}}
        manifest = {"vccp": {"revision": _checkout_head()}}
        output = StringIO()
        runtime = type("Runtime", (), {"local_reviewer": Reviewer()})()
        with patch.object(runner, "_load", side_effect=[manifest, local]), \
                patch.object(runner, "_runtime_source_revision", return_value=_checkout_head()), \
                patch.object(runner, "_workspace_repository", return_value="owner/repo"), \
                patch.object(runner, "RuntimeLock", Lock), \
                patch.object(runner, "build_runtime", return_value=runtime), redirect_stdout(output):
            result = runner.main(["--manifest", "manifest.yml", "--runtime-config", "runtime.json",
                                  "--repo", "owner/repo", "--retry-reviewer", "--issue", "7",
                                  "--pr", "12", "--head-sha", head,
                                  "--authorize-reviewer-retry-for", head])

        self.assertEqual(result, 0)
        self.assertEqual(events, [("identity", "owner/repo", "state.db"), "lock",
                                  ("retry", "owner/repo", 7, 12, head, head), "release"])
        self.assertEqual(json.loads(output.getvalue())["execution_id"], "claude-session")

    def test_repair_continuation_cli_binds_explicit_target_and_persisted_cause(self):
        head = "a" * 40
        events = []
        class Lock:
            def __init__(self, repo, database): events.append(("identity", repo, database))
            def __enter__(self): events.append("lock"); return self
            def __exit__(self, *_): events.append("release")
        class Store:
            def attempts_for_repo(self, repo):
                return [{"issue_number": 57, "pr_number": 58, "expected_head_sha": head,
                         "phase": "BUDGET_EXHAUSTED", "cause_type": "reviewer_rejection",
                         "cause_id": "review-123"}]
        class Core:
            store = Store()
            def authorize_repair_continuation(self, candidate, authorized_by):
                events.append(("authorize", candidate.repo, candidate.issue_number, candidate.pr_number,
                               candidate.head_sha, candidate.cause_type, candidate.cause_id,
                               candidate.branch, authorized_by))
                return {"status": "authorized", "authorization_id": "auth-1"}
        workflow = SimpleNamespace(observe=lambda repo, issue: SimpleNamespace(
            pr_number=58, pr_branch="implement/57"))
        runtime = SimpleNamespace(core=Core(), workflow=workflow)
        local = {"database_path": "state.db", "owner_id": "owner", "workspace": ".",
                 "target_repository": "owner/repo", "enrollment": {"authorized_repository": "owner/repo"}}
        manifest = {"vccp": {"revision": _checkout_head()}}
        output = StringIO()
        with patch.object(runner, "_load", side_effect=[manifest, local]), \
             patch.object(runner, "_runtime_source_revision", return_value=_checkout_head()), \
             patch.object(runner, "_workspace_repository", return_value="owner/repo"), \
             patch.object(runner, "RuntimeLock", Lock), \
             patch.object(runner, "build_runtime", return_value=runtime), redirect_stdout(output):
            result = runner.main(["--manifest", "manifest.yml", "--runtime-config", "runtime.json",
                                  "--repo", "owner/repo", "--authorize-repair-continuation",
                                  "--issue", "57", "--pr", "58", "--head-sha", head,
                                  "--authorized-by", "maintainer"])
        self.assertEqual(result, 0)
        self.assertEqual(events[-2], ("authorize", "owner/repo", 57, 58, head,
                                      "reviewer_rejection", "review-123", "implement/57", "maintainer"))
        self.assertEqual(events[-1], "release")
        self.assertEqual(json.loads(output.getvalue())["status"], "authorized")

    def test_cli_rejects_target_or_enrollment_mismatch_before_runtime_construction(self):
        manifest = {"vccp": {"revision": _checkout_head()}}
        local = {"database_path": "state.db", "owner_id": "owner", "workspace": ".",
                 "target_repository": "other/repo", "enrollment": {"authorized_repository": "owner/repo"}}
        with patch.object(runner, "_load", side_effect=[manifest, local]), \
             patch.object(runner, "_runtime_source_revision", return_value=_checkout_head()), \
             patch.object(runner, "build_runtime", side_effect=AssertionError("must not construct runtime")):
            with self.assertRaises(SystemExit) as error:
                runner.main(["--manifest", "manifest.json", "--runtime-config", "runtime.json",
                             "--repo", "owner/repo"])
        self.assertEqual(error.exception.code, 2)
    def test_load_supports_json_and_yaml(self):
        with tempfile.TemporaryDirectory() as folder:
            json_file = Path(folder) / "config.json"
            json_file.write_text('{"key": "value_json"}', encoding="utf-8")
            self.assertEqual(runner._load(str(json_file)), {"key": "value_json"})

            yaml_file = Path(folder) / "control-plane.yml"
            yaml_file.write_text("schema_version: 2\nkey: value_yaml\n", encoding="utf-8")
            self.assertEqual(runner._load(str(yaml_file)), {"schema_version": 2, "key": "value_yaml"})

    def test_onboarding_schema_v2_manifest_crosses_production_runner_boundary(self):
        fixture = Path(__file__).parent / "fixtures" / "consumer-control-plane-v2.yml"
        manifest = runner._load(str(fixture))

        self.assertEqual(manifest["schema_version"], 2)
        self.assertEqual(manifest["vccp"]["source"], "liuchangchxy/vibe-coding-control-plane")
        self.assertEqual(manifest["repository"]["base_branch"], "main")
        self.assertEqual(manifest["issue_contract"]["max_automated_repairs"], 3)
        self.assertTrue(manifest["reviewer"]["exact_head_sha_required"])
        self.assertEqual(manifest["reviewer"]["required_checks"][0]["name"], "test")
        self.assertEqual(manifest["merge"]["method"], "squash")
        self.assertEqual(RuntimeConfig.from_manifest(manifest).max_automated_repairs, 5)

if __name__ == "__main__":
    raise SystemExit(unittest.main())
