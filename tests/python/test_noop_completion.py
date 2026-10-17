import hashlib
import subprocess
import tempfile
import unittest
from unittest import mock
from dataclasses import replace
from pathlib import Path

from vccp_runtime.core import LaunchDisposition, LaunchResult, RuntimeCore, WorkflowSnapshot


BODY = "completion_policy: allow_no_change=true\nFrozen task details."
DIGEST = hashlib.sha256(BODY.encode()).hexdigest()
MANIFEST = {"schema_version": 2, "issue_contract": {"max_automated_repairs": 3}}


class Workflow:
    def __init__(self):
        self.snapshot = WorkflowSnapshot("acme/repo", 7, "r1", True, "agent-ready", True,
                                         allow_no_change=True, frozen_spec_digest=DIGEST)
        self.closes = 0
        self.clears = 0
        self.closeout_intent_at_close = None
        self.store = None

    def observe(self, repo, issue):
        return self.snapshot

    def transition_coordination_state(self, *args):
        self.snapshot = replace(self.snapshot, coordination_state=args[3], revision="r2")
        return True

    def close_noop_issue(self, repo, issue):
        self.closes += 1
        self.closeout_intent_at_close = self.store.attempt(self.attempt_id)["no_op_closeout_intent"]
        self.snapshot = replace(self.snapshot, issue_open=False, coordination_state="invalid", active_labels=())
        return True

    def clear_active_coordination_labels(self, repo, issue):
        self.clears += 1
        return True

    def discover_active(self, repo):
        return [7]


class Implementer:
    def __init__(self, disposition=LaunchDisposition.CONFIRMED):
        self.workspace = None
        self.disposition = disposition

    def launch(self, request):
        return LaunchResult(self.disposition, "execution-1" if self.disposition == LaunchDisposition.CONFIRMED else None,
                            "antigravity" if self.disposition == LaunchDisposition.CONFIRMED else None)

    def latest_activity_for(self, provider, execution_id):
        return None


class NoOpCompletionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "runtime.sqlite"
        self.workflow = Workflow()
        self.core = RuntimeCore(self.db, MANIFEST, self.workflow, Implementer())
        self.attempt = self.core.dispatch_initial("acme/repo", 7, "worker")["attempt_id"]
        self.workflow.store = self.core.store
        self.workflow.attempt_id = self.attempt

    def tearDown(self):
        self.temp.cleanup()

    def receipt(self, **kwargs):
        return self.core.store.record_completion(self.attempt, "antigravity", "execution-1", "{}", **kwargs)

    def test_authorized_noop_completes_once_and_survives_restart(self):
        self.assertTrue(self.receipt())
        restarted = RuntimeCore(self.db, MANIFEST, self.workflow, Implementer())
        self.assertEqual(restarted.reconcile_no_change("acme/repo")["items"][0]["status"], "success_no_op")
        self.assertEqual(restarted.store.attempt(self.attempt)["phase"], "SUCCESS_NO_OP")
        self.assertEqual(self.workflow.closes, 1)

    def test_restart_after_github_close_before_sqlite_terminal_write(self):
        self.assertTrue(self.receipt())
        with mock.patch.object(self.core.store, "no_op_terminal", side_effect=RuntimeError("simulated crash")):
            first_result = self.core.reconcile_no_change("acme/repo")
        self.assertEqual(self.workflow.closeout_intent_at_close, 1)
        self.assertEqual(self.core.store.attempt(self.attempt)["phase"], "LAUNCH_CONFIRMED")
        self.assertEqual(self.workflow.snapshot.issue_open, False)
        restarted = RuntimeCore(self.db, MANIFEST, self.workflow, Implementer())
        result = restarted.reconcile_no_change("acme/repo")
        self.assertEqual(result["items"][0]["status"], "success_no_op")
        self.assertEqual(restarted.store.attempt(self.attempt)["phase"], "SUCCESS_NO_OP")

    def test_external_close_before_runtime_close_intent_is_not_successful_noop(self):
        self.assertTrue(self.receipt())
        self.workflow.snapshot = replace(self.workflow.snapshot, issue_open=False,
                                          coordination_state="invalid", active_labels=())
        result = self.core.reconcile_no_change("acme/repo")
        self.assertEqual(result["items"][0]["status"], "no_op_deferred")
        self.assertEqual(self.core.store.attempt(self.attempt)["phase"], "LAUNCH_CONFIRMED")
        self.assertEqual(self.core.store.attempt(self.attempt)["no_op_closeout_intent"], 0)

    def test_no_receipt_does_not_prevent_existing_stall_timeout(self):
        row = self.core.store.attempt(self.attempt)
        result = self.core.observe_implementer_progress("acme/repo", row["progress_deadline_at"])
        self.assertEqual(result["items"][0]["status"], "implementer_stalled")

    def test_linked_pr_blocks_noop(self):
        self.assertTrue(self.receipt())
        self.workflow.snapshot = replace(self.workflow.snapshot, canonical_link_count=1)
        self.assertEqual(self.core.reconcile_no_change("acme/repo")["items"][0]["status"], "no_op_deferred")
        self.assertEqual(self.workflow.closes, 0)

    def test_wrong_attempt_provider_and_execution_are_rejected(self):
        self.assertFalse(self.core.store.record_completion("stale", "antigravity", "execution-1", "{}"))
        self.assertFalse(self.core.store.record_completion(self.attempt, "claude_code", "execution-1", "{}"))
        self.assertFalse(self.core.store.record_completion(self.attempt, "antigravity", "other", "{}"))

    def test_frozen_spec_drift_is_rejected(self):
        self.assertTrue(self.receipt())
        self.workflow.snapshot = replace(self.workflow.snapshot, frozen_spec_digest="different")
        self.assertEqual(self.core.reconcile_no_change("acme/repo")["items"][0]["status"], "no_op_deferred")

    def test_authorization_revocation_and_needs_human_block_completion(self):
        self.assertTrue(self.receipt())
        self.workflow.snapshot = replace(self.workflow.snapshot, allow_no_change=False)
        self.assertEqual(self.core.reconcile_no_change("acme/repo")["items"][0]["status"], "no_op_deferred")
        self.workflow.snapshot = replace(self.workflow.snapshot, allow_no_change=True, terminal_labels=("needs-human",))
        self.assertEqual(self.core.reconcile_no_change("acme/repo")["items"][0]["status"], "no_op_deferred")

    def test_receipt_with_different_provenance_cannot_replace_existing_receipt(self):
        self.assertTrue(self.receipt())
        self.assertFalse(self.core.store.record_completion(self.attempt, "antigravity", "execution-1", "different"))

    def test_workspace_mutation_is_rejected(self):
        workspace = Path(self.temp.name) / "consumer"
        workspace.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=workspace, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=workspace, check=True)
        (workspace / "tracked.txt").write_text("baseline")
        subprocess.run(["git", "add", "tracked.txt"], cwd=workspace, check=True)
        subprocess.run(["git", "commit", "-qm", "baseline"], cwd=workspace, check=True)
        self.core.workspace_path = str(workspace)
        baseline = self.core._workspace_baseline(self.core.workspace_path)
        self.core.store.bind_noop_authorization(self.attempt, True, DIGEST, baseline)
        (workspace / "tracked.txt").write_text("changed")
        self.assertTrue(self.receipt())
        self.assertEqual(self.core.reconcile_no_change("acme/repo")["items"][0]["status"], "workspace_mutated")

    def test_historical_recovery_is_exact_and_records_evidence_before_closeout(self):
        attempt = "8dc7fdbc-39c2-4fc1-9193-aab3d060f2a0"
        execution = "c6d4791b-419b-4800-a83d-f229761a8dab"
        connection = self.core.store.transaction()
        try:
            connection.execute("INSERT INTO attempts(attempt_id,repo,issue_number,owner_id,lease_token,kind,phase,"
                               "launch_outcome,execution_id,implementer_provider,created_at,updated_at) "
                               "VALUES(?, 'liuchangchxy/dayspark',37,'worker','', 'initial_dispatch',"
                               "'TERMINAL_UNRESOLVED','confirmed',?,'antigravity',1,1)", (attempt,execution))
            connection.commit()
        finally:
            connection.close()
        workspace = Path(self.temp.name) / "historical"
        workspace.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=workspace, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=workspace, check=True)
        (workspace / "tracked.txt").write_text("historical")
        subprocess.run(["git", "add", "tracked.txt"], cwd=workspace, check=True)
        subprocess.run(["git", "commit", "-qm", "historical"], cwd=workspace, check=True)
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=workspace, check=True,
                              capture_output=True, text=True).stdout.strip()
        self.core.workspace_path = str(workspace)
        self.workflow.snapshot = WorkflowSnapshot("liuchangchxy/dayspark", 37, "r1", True,
            "infra-blocked", True, terminal_labels=("infra-blocked",))
        self.assertEqual(self.core.recover_historical_dayspark37("wrong/repo", attempt, head, "evidence", "a"*64)["status"],
                         "invalid_historical_recovery_request")
        result = self.core.recover_historical_dayspark37("liuchangchxy/dayspark", attempt, head,
                                                         "local://evidence", "a"*64)
        self.assertEqual(result["status"], "receipt_recorded")
        self.assertEqual(self.core.store.receipt(attempt)["source"], "operator_recovery")
        self.assertEqual(self.core.store.attempt(attempt)["phase"], "TERMINAL_UNRESOLVED")


if __name__ == "__main__":
    unittest.main()
