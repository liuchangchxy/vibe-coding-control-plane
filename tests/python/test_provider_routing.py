"""P5-A1 provider foundation: deterministic routing and durable provider provenance."""
import json
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from vccp_runtime.adapters import build_runtime
from vccp_runtime.core import (LaunchDisposition, LaunchRequest, LaunchResult, RuntimeCore,
                               WorkflowSnapshot)
from vccp_runtime.providers import ANTIGRAVITY_PROVIDER, ProviderRouter


REPO = "acme/alpha"
ISSUE = 7
SHA = "a" * 40
BRANCH = "implement/7"
CONVERSATION_ID = "e13f972a-83b8-4f8a-9a6f-8c7d3b2a1f05"
CORE_MANIFEST = {"schema_version": 2, "issue_contract": {"max_automated_repairs": 3}}
PRODUCTION_MANIFEST = {
    "schema_version": 2,
    "issue_contract": {"max_automated_repairs": 3, "frozen_spec_label": "frozen-spec",
                       "active_coordination_labels": ["agent-ready", "agent-working", "changes-requested"],
                       "terminal_coordination_labels": ["infra-blocked", "needs-human"]},
    "repository": {"base_branch": "main", "implementer_authors": ["agent"]},
    "reviewer": {"required_checks": [{"name": "Unit Tests", "accepted_conclusions": ["success"]}]},
}


def launch_request(attempt_id="attempt-1"):
    return LaunchRequest(REPO, ISSUE, attempt_id, "initial_dispatch")


class StubProvider:
    """A provider adapter with a fully deterministic launch and activity surface."""

    def __init__(self, key, results=(), activity=None, failure=None):
        self.key = key
        self.results = list(results)
        self.activity = activity
        self.failure = failure
        self.requests = []
        self.activity_calls = []

    def launch(self, request):
        self.requests.append(request)
        if self.failure is not None:
            raise self.failure
        return self.results.pop(0) if self.results else LaunchResult(
            LaunchDisposition.CONFIRMED, f"{self.key}-execution")

    def latest_activity(self, execution_id):
        self.activity_calls.append(execution_id)
        return self.activity


def snap(repo=REPO, issue=ISSUE, state="agent-ready", revision="1"):
    return WorkflowSnapshot(repo=repo, issue_number=issue, revision=revision, issue_open=True,
                            coordination_state=state, frozen_spec=True)


class StubWorkflow:
    def __init__(self):
        self.current = snap()
        self.transitions = []

    def observe(self, repo, issue_number):
        return self.current

    def discover_active(self, repo):
        return []

    def transition_coordination_state(self, repo, issue_number, expected_state, new_state, expected_revision):
        self.transitions.append((expected_state, new_state))
        if self.current.coordination_state != expected_state or self.current.revision != expected_revision:
            return False
        self.current = replace(self.current, coordination_state=new_state,
                               revision=f"{self.current.revision}+")
        return True


class ProviderRouterTests(unittest.TestCase):
    def test_primary_confirmed_never_consults_fallback(self):
        primary = StubProvider("primary", [LaunchResult(LaunchDisposition.CONFIRMED, "exec-1")])
        fallback = StubProvider("fallback")
        result = ProviderRouter([("primary", primary), ("fallback", fallback)]).launch(launch_request())
        self.assertEqual((result.disposition, result.execution_id, result.provider),
                         (LaunchDisposition.CONFIRMED, "exec-1", "primary"))
        self.assertEqual(fallback.requests, [])

    def test_primary_unknown_never_consults_fallback(self):
        primary = StubProvider("primary", [LaunchResult(LaunchDisposition.UNKNOWN)])
        fallback = StubProvider("fallback")
        result = ProviderRouter([("primary", primary), ("fallback", fallback)]).launch(launch_request())
        self.assertEqual((result.disposition, result.provider), (LaunchDisposition.UNKNOWN, "primary"))
        self.assertEqual(fallback.requests, [])

    def test_definitely_not_started_primary_reaches_confirmed_fallback(self):
        primary = StubProvider("primary", [LaunchResult(LaunchDisposition.DEFINITELY_NOT_STARTED)])
        fallback = StubProvider("fallback", [LaunchResult(LaunchDisposition.CONFIRMED, "fallback-exec")])
        result = ProviderRouter([("primary", primary), ("fallback", fallback)]).launch(launch_request())
        self.assertEqual((result.disposition, result.execution_id, result.provider),
                         (LaunchDisposition.CONFIRMED, "fallback-exec", "fallback"))
        self.assertEqual(len(primary.requests), 1)
        self.assertEqual(len(fallback.requests), 1)

    def test_unstarted_primary_with_unknown_fallback_stops_there(self):
        primary = StubProvider("primary", [LaunchResult(LaunchDisposition.DEFINITELY_NOT_STARTED)])
        fallback = StubProvider("fallback", [LaunchResult(LaunchDisposition.UNKNOWN)])
        result = ProviderRouter([("primary", primary), ("fallback", fallback)]).launch(launch_request())
        self.assertEqual((result.disposition, result.provider), (LaunchDisposition.UNKNOWN, "fallback"))
        self.assertEqual(len(fallback.requests), 1)

    def test_all_providers_definitely_not_started(self):
        primary = StubProvider("primary", [LaunchResult(LaunchDisposition.DEFINITELY_NOT_STARTED)])
        fallback = StubProvider("fallback", [LaunchResult(LaunchDisposition.DEFINITELY_NOT_STARTED)])
        result = ProviderRouter([("primary", primary), ("fallback", fallback)]).launch(launch_request())
        self.assertEqual((result.disposition, result.execution_id, result.provider),
                         (LaunchDisposition.DEFINITELY_NOT_STARTED, None, None))
        self.assertEqual((len(primary.requests), len(fallback.requests)), (1, 1))

    def test_provider_failure_is_unknown_owned_by_that_provider(self):
        primary = StubProvider("primary", failure=RuntimeError("adapter exploded"))
        fallback = StubProvider("fallback", [LaunchResult(LaunchDisposition.CONFIRMED, "fallback-exec")])
        result = ProviderRouter([("primary", primary), ("fallback", fallback)]).launch(launch_request())
        self.assertEqual((result.disposition, result.provider), (LaunchDisposition.UNKNOWN, "primary"))
        self.assertEqual(fallback.requests, [])

    def test_activity_is_observed_only_through_the_named_provider(self):
        primary = StubProvider("primary", activity=99.0)
        owner = StubProvider("owner", activity=5.0)
        router = ProviderRouter([("primary", primary), ("owner", owner)])
        self.assertEqual(router.latest_activity_for("owner", "exec-1"), 5.0)
        self.assertEqual(owner.activity_calls, ["exec-1"])
        self.assertEqual(primary.activity_calls, [])

    def test_missing_or_unregistered_provenance_fails_closed(self):
        primary = StubProvider("primary", activity=99.0)
        router = ProviderRouter([("primary", primary)])
        for provider in (None, "unregistered"):
            with self.subTest(provider=provider):
                self.assertIsNone(router.latest_activity_for(provider, "exec-1"))
        self.assertEqual(primary.activity_calls, [])

    def test_invalid_provider_registrations_are_rejected(self):
        provider = StubProvider("primary")
        with self.assertRaisesRegex(ValueError, "at least one implementer provider"):
            ProviderRouter([])
        with self.assertRaisesRegex(ValueError, "duplicate implementer provider key"):
            ProviderRouter([("primary", provider), ("primary", StubProvider("other"))])
        with self.assertRaisesRegex(ValueError, "key must be a lowercase identifier"):
            ProviderRouter([("Primary", provider)])


class DurableProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "runtime.sqlite3"
        self.workflow = StubWorkflow()

    def tearDown(self):
        self.temp.cleanup()

    def runtime(self, *providers):
        self.router = ProviderRouter([(provider.key, provider) for provider in providers])
        return RuntimeCore(self.db, CORE_MANIFEST, self.workflow, self.router,
                           recovery_timeout_seconds=900, implementer_progress_timeout_seconds=1800)

    def test_confirmed_launch_stores_provider_with_execution_and_survives_restart(self):
        primary = StubProvider("antigravity", [LaunchResult(LaunchDisposition.CONFIRMED, "exec-9")])
        core = self.runtime(primary)
        result = core.dispatch_initial(REPO, ISSUE, "owner")
        row = core.store.attempt(result["attempt_id"])
        self.assertEqual((row["implementer_provider"], row["execution_id"], row["launch_outcome"]),
                         ("antigravity", "exec-9", "confirmed"))
        restarted = RuntimeCore(self.db, CORE_MANIFEST, self.workflow,
                                ProviderRouter([("antigravity", StubProvider("antigravity"))]))
        self.assertEqual(restarted.store.attempt(result["attempt_id"])["implementer_provider"], "antigravity")

    def test_unknown_launch_stores_its_provider_and_never_reroutes(self):
        primary = StubProvider("antigravity", [LaunchResult(LaunchDisposition.UNKNOWN)])
        fallback = StubProvider("claude_code")
        core = self.runtime(primary, fallback)
        result = core.dispatch_initial(REPO, ISSUE, "owner")
        row = core.store.attempt(result["attempt_id"])
        self.assertEqual(result["status"], "launch_unresolved")
        self.assertEqual((row["phase"], row["implementer_provider"], row["execution_id"], row["launch_outcome"]),
                         ("LAUNCH_UNKNOWN", "antigravity", None, "unknown"))
        self.assertEqual(fallback.requests, [])
        self.workflow.current = snap(revision="2")
        self.assertEqual(core.dispatch_initial(REPO, ISSUE, "owner")["status"],
                         "initial_attempt_already_recorded")
        self.assertEqual((len(primary.requests), len(fallback.requests)), (1, 0))

    def test_unknown_from_fallback_provider_is_recorded_without_rerouting(self):
        primary = StubProvider("antigravity", [LaunchResult(LaunchDisposition.DEFINITELY_NOT_STARTED)])
        fallback = StubProvider("claude_code", [LaunchResult(LaunchDisposition.UNKNOWN)])
        core = self.runtime(primary, fallback)
        result = core.dispatch_initial(REPO, ISSUE, "owner")
        row = core.store.attempt(result["attempt_id"])
        self.assertEqual((row["phase"], row["implementer_provider"]), ("LAUNCH_UNKNOWN", "claude_code"))

    def test_all_providers_unstarted_records_no_provider_provenance(self):
        primary = StubProvider("antigravity", [LaunchResult(LaunchDisposition.DEFINITELY_NOT_STARTED)])
        fallback = StubProvider("claude_code", [LaunchResult(LaunchDisposition.DEFINITELY_NOT_STARTED)])
        core = self.runtime(primary, fallback)
        result = core.dispatch_initial(REPO, ISSUE, "owner")
        row = core.store.attempt(result["attempt_id"])
        self.assertEqual((result["status"], row["phase"], row["implementer_provider"], row["execution_id"]),
                         ("launch_not_started", "LAUNCH_NOT_STARTED", None, None))
        self.assertEqual((len(primary.requests), len(fallback.requests)), (1, 1))

    def test_restart_observes_activity_through_stored_provider_not_configured_primary(self):
        owner = StubProvider("antigravity", [LaunchResult(LaunchDisposition.CONFIRMED, "ag-exec")])
        core = self.runtime(owner)
        result = core.dispatch_initial(REPO, ISSUE, "owner")
        baseline = core.store.attempt(result["attempt_id"])["implementer_activity_at"]
        owner.activity = baseline + 30
        current_primary = StubProvider("claude_code", activity=baseline + 5000)
        restarted = RuntimeCore(self.db, CORE_MANIFEST, self.workflow,
                                ProviderRouter([("claude_code", current_primary), ("antigravity", owner)]))
        observed = restarted.observe_implementer_progress(REPO, baseline + 31)
        self.assertEqual(observed["items"][0]["status"], "progress")
        self.assertEqual(owner.activity_calls, ["ag-exec"])
        self.assertEqual(current_primary.activity_calls, [])
        self.assertEqual(restarted.store.attempt(result["attempt_id"])["implementer_activity_at"], baseline + 30)

    def test_restart_without_the_stored_provider_fails_closed(self):
        owner = StubProvider("antigravity", [LaunchResult(LaunchDisposition.CONFIRMED, "ag-exec")])
        core = self.runtime(owner)
        result = core.dispatch_initial(REPO, ISSUE, "owner")
        baseline = core.store.attempt(result["attempt_id"])["implementer_activity_at"]
        current_primary = StubProvider("claude_code", activity=baseline + 5000)
        restarted = RuntimeCore(self.db, CORE_MANIFEST, self.workflow,
                                ProviderRouter([("claude_code", current_primary)]))
        observed = restarted.observe_implementer_progress(REPO, baseline + 31)
        self.assertEqual(observed["items"][0]["status"], "no_new_activity")
        self.assertEqual(current_primary.activity_calls, [])


class LegacyDatabaseMigrationTests(unittest.TestCase):
    """Pre-P5 databases upgrade in place without inventing provider provenance."""

    LEGACY_ROWS = (
        # attempt_id, kind, phase, launch_outcome, execution_id, extra columns
        ("launched-confirmed", "initial_dispatch", "LAUNCH_CONFIRMED", "confirmed", "exec-1", {}),
        ("launched-bound", "initial_dispatch", "PR_BOUND", "confirmed", "exec-1",
         {"pr_number": 44, "branch": BRANCH, "resulting_head_sha": SHA}),
        ("launched-unknown", "repair", "LAUNCH_UNKNOWN", "unknown", None,
         {"repair_ordinal": 1, "repair_key": f"{REPO}:{SHA}"}),
        ("adopted", "adopted", "PR_BOUND", None, None,
         {"pr_number": 44, "branch": BRANCH, "resulting_head_sha": SHA}),
        ("budget-exhausted", "repair", "BUDGET_EXHAUSTED", None, None,
         {"repair_ordinal": 2, "repair_key": f"{REPO}:{BRANCH}"}),
        ("claim-failed", "initial_dispatch", "CLAIM_FAILED", "claim_rejected", None, {}),
        ("never-launched", "initial_dispatch", "LAUNCH_INTENT", None, None, {}),
    )
    EXPECTED_PROVIDERS = {
        "launched-confirmed": "antigravity", "launched-bound": "antigravity",
        "launched-unknown": "antigravity", "adopted": None, "budget-exhausted": None,
        "claim-failed": None, "never-launched": None,
    }

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "legacy.sqlite3"
        self.workflow = StubWorkflow()

    def tearDown(self):
        self.temp.cleanup()

    def open_runtime(self):
        return RuntimeCore(self.db, CORE_MANIFEST, self.workflow,
                           ProviderRouter([("antigravity", StubProvider("antigravity"))]))

    def insert(self, connection, attempt_id, kind, phase, outcome, execution_id, extra):
        columns = {"attempt_id": attempt_id, "repo": REPO, "issue_number": ISSUE, "owner_id": "owner",
                   "lease_token": "", "kind": kind, "phase": phase, "launch_outcome": outcome,
                   "execution_id": execution_id, "created_at": 1.0, "updated_at": 1.0,
                   "phase_entered_at": 1.0, **extra}
        connection.execute(f"INSERT INTO attempts({','.join(columns)}) "
                           f"VALUES({','.join('?' * len(columns))})", tuple(columns.values()))

    def build_legacy_database(self):
        """Create the pre-P5 shape: current schema minus the provider column, plus legacy rows."""
        self.open_runtime()
        with closing(sqlite3.connect(self.db)) as connection:
            connection.execute("ALTER TABLE attempts DROP COLUMN implementer_provider")
            for row in self.LEGACY_ROWS:
                self.insert(connection, *row)
            connection.commit()

    def providers_by_attempt(self, core):
        return {row["attempt_id"]: row["implementer_provider"]
                for row in core.store.attempts_for_repo(REPO)}

    def test_legacy_launch_provenance_is_backfilled_without_touching_other_rows(self):
        self.build_legacy_database()
        core = self.open_runtime()
        self.assertEqual(self.providers_by_attempt(core), self.EXPECTED_PROVIDERS)

    def test_migration_preserves_existing_attempt_and_repair_evidence(self):
        self.build_legacy_database()
        core = self.open_runtime()
        rows = {row["attempt_id"]: row for row in core.store.attempts_for_repo(REPO)}
        self.assertEqual(rows["launched-confirmed"]["execution_id"], "exec-1")
        self.assertEqual(rows["launched-confirmed"]["phase"], "LAUNCH_CONFIRMED")
        self.assertEqual((rows["launched-bound"]["pr_number"], rows["launched-bound"]["resulting_head_sha"]),
                         (44, SHA))
        self.assertEqual(rows["launched-unknown"]["phase"], "LAUNCH_UNKNOWN")
        self.assertEqual(rows["budget-exhausted"]["phase"], "BUDGET_EXHAUSTED")
        self.assertEqual(rows["adopted"]["kind"], "adopted")
        self.assertEqual(len(core.store.attempts_for_repo(REPO)), len(self.LEGACY_ROWS))

    def test_migration_is_idempotent_and_never_reattributes_later_rows(self):
        self.build_legacy_database()
        self.open_runtime()
        with closing(sqlite3.connect(self.db)) as connection:
            # A post-P5 outcome that recorded no owning provider must stay NULL.
            self.insert(connection, "post-p5-unknown", "initial_dispatch", "LAUNCH_UNKNOWN", "unknown",
                        None, {})
            connection.commit()
        core = self.open_runtime()
        providers = self.providers_by_attempt(core)
        self.assertIsNone(providers["post-p5-unknown"])
        self.assertEqual(providers, {**self.EXPECTED_PROVIDERS, "post-p5-unknown": None})


class ProductionWiringTests(unittest.TestCase):
    """build_runtime routes AntiGravity through the registry without breaking legacy configuration."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.workspace = self.init_git_repo(Path(self.temp.name) / "repo")
        self.api = FakeGitHub()
        self.writer = MutatingWriter(self.api)
        self.launches = []

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def init_git_repo(path):
        path.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init", str(path)], check=True, capture_output=True, text=True)
        subprocess.run(["git", "-C", str(path), "-c", "user.name=VCCP Test", "-c",
                        "user.email=vccp-test@example.invalid", "commit", "--allow-empty",
                        "-m", "baseline"], check=True, capture_output=True, text=True)
        return path

    def legacy_config(self, **overrides):
        config = {"database_path": str(Path(self.temp.name) / "runtime.sqlite"),
                  "workspace": str(self.workspace), "owner_id": "owner",
                  "antigravity_executable": "language_server.exe",
                  "app_gh_executable": "app-gh.exe", "app_git_push_executable": "app-git-push.exe",
                  "github_read_token_env": "TOKEN"}
        return {**config, **overrides}

    def agent_runner(self, args, **kwargs):
        self.assertEqual(args[:3], ["language_server.exe", "agentapi", "new-conversation"])
        self.launches.append(list(args))
        return SimpleNamespace(returncode=0, stdout=json.dumps({"conversation_id": CONVERSATION_ID}))

    def test_legacy_antigravity_only_configuration_still_builds_a_working_runtime(self):
        wired = build_runtime(PRODUCTION_MANIFEST, self.legacy_config(), api=self.api,
                              writer=self.writer, runner=self.agent_runner)
        self.assertIsInstance(wired.router, ProviderRouter)
        self.assertEqual(wired.router.primary, ANTIGRAVITY_PROVIDER)
        result = wired.core.dispatch_initial("o/r", 1, "owner")
        self.assertEqual(result["phase"], "LAUNCH_CONFIRMED")
        row = wired.core.store.attempt(result["attempt_id"])
        self.assertEqual((row["implementer_provider"], row["execution_id"]), ("antigravity", CONVERSATION_ID))
        self.assertEqual(len(self.launches), 1)

    def test_configured_provider_list_is_used_in_order(self):
        configured = self.legacy_config(providers=[
            {"key": "antigravity", "type": "antigravity", "executable": "language_server.exe"}])
        wired = build_runtime(PRODUCTION_MANIFEST, configured, api=self.api,
                              writer=self.writer, runner=self.agent_runner)
        self.assertEqual(wired.router.primary, "antigravity")
        self.assertEqual(wired.core.dispatch_initial("o/r", 1, "owner")["status"], "launched")

    def test_unimplemented_provider_type_fails_closed(self):
        configured = self.legacy_config(providers=[{"key": "claude_code", "type": "claude_code"}])
        with self.assertRaisesRegex(ValueError, "unsupported implementer provider type: claude_code"):
            build_runtime(PRODUCTION_MANIFEST, configured, api=self.api, writer=self.writer)


class FakeGitHub:
    def __init__(self):
        self.issue = {"number": 1, "state": "open", "updated_at": "r1",
                      "labels": [{"name": "agent-ready"}, {"name": "frozen-spec"}]}

    def get(self, path):
        return self.issue

    def graphql(self, query, variables):
        return {"repository": {"issue": {"timelineItems": {"nodes": [],
                                                           "pageInfo": {"hasNextPage": False}}}}}


class MutatingWriter:
    def __init__(self, api):
        self.api = api

    def replace_labels(self, repo, issue, remove, add):
        self.api.issue["labels"] = [item for item in self.api.issue["labels"] if item["name"] not in remove]
        self.api.issue["labels"].extend({"name": label} for label in add)
        self.api.issue["updated_at"] += "+"
        return True


if __name__ == "__main__":
    unittest.main()
