import tempfile
import unittest
from contextlib import closing
from dataclasses import replace
from pathlib import Path

from vccp_runtime.core import (LaunchDisposition, LaunchResult, RuntimeCore, WorkflowSnapshot)
from vccp_runtime.lifecycle import LifecycleDriver


SHA = "a" * 40
SHA_B = "b" * 40
BASE_MANIFEST = {
    "schema_version": 2,
    "issue_contract": {"max_automated_repairs": 3},
    "reviewer": {"required_checks": [{"name": "Unit", "accepted_conclusions": ["success"],
                                        "implementation_failure_conclusions": ["failure"],
                                        "infrastructure_failure_conclusions": ["timed_out"]}]},
}


class FakeWorkflow:
    def __init__(self):
        self.snapshots = {}
        self.transitions = []
        self.cleared = []

    def observe(self, repo, issue):
        return self.snapshots[(repo, issue)]

    def transition_coordination_state(self, repo, issue, expected_state, new_state, expected_revision):
        snapshot = self.observe(repo, issue)
        if snapshot.revision != expected_revision or snapshot.coordination_state != expected_state:
            return False
        terminal = (new_state,) if new_state in {"infra-blocked", "needs-human"} else ()
        self.snapshots[(repo, issue)] = replace(snapshot, revision=snapshot.revision + "+",
            coordination_state=new_state, terminal_labels=terminal)
        self.transitions.append((repo, issue, new_state))
        return True

    def discover_active(self, repo):
        return sorted(issue for (key, issue), snapshot in self.snapshots.items()
                      if key == repo and snapshot.coordination_state in {"agent-working", "changes-requested"})

    def clear_active_coordination_labels(self, repo, issue):
        self.cleared.append((repo, issue))
        snapshot = self.observe(repo, issue)
        self.snapshots[(repo, issue)] = replace(snapshot, active_labels=())
        return True


class FakeImplementer:
    def __init__(self): self.launches = []
    def launch(self, request):
        self.launches.append(request)
        return LaunchResult(LaunchDisposition.CONFIRMED, f"exec-{len(self.launches)}")


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "runtime.sqlite"
        self.workflow = FakeWorkflow()
        self.implementer = FakeImplementer()
        self.repo = "acme/alpha"
        self.issue = 7
        self.manifest = BASE_MANIFEST
        self.core = RuntimeCore(self.db, self.manifest, self.workflow, self.implementer,
                                lease_ttl_seconds=300, recovery_timeout_seconds=900)
        self._snapshot(self.repo, self.issue)
        initial = self.core.dispatch_initial(self.repo, self.issue, "owner")
        self.assertEqual(initial["status"], "launched")
        snap = self.workflow.observe(self.repo, self.issue)
        snap = replace(snap, canonical_relationship_valid=True, canonical_link_count=1,
                       open_linked_pr_count=1, pr_number=44, pr_open=True, pr_state="open",
                       pr_linked_issue=self.issue, pr_head_sha=SHA, pr_branch="implement/7",
                       active_labels=("agent-working",))
        self.workflow.snapshots[(self.repo, self.issue)] = snap
        attempt = self.core.store.attempts_for_repo(self.repo)[0]
        self.core.store.bind_pr(attempt["attempt_id"], 44, "implement/7", SHA, 1)

    def tearDown(self): self.temp.cleanup()

    def _snapshot(self, repo, issue, **changes):
        snap = WorkflowSnapshot(repo, issue, "r1", True, "agent-ready", True)
        self.workflow.snapshots[(repo, issue)] = replace(snap, **changes)

    def driver(self, manifest=None, timeouts=None, core=None):
        return LifecycleDriver(core or self.core, manifest or self.manifest,
                               timeouts or {"waiting_ci": 10, "waiting_review": 10, "waiting_merge": 10})

    def test_empty_required_check_configuration_cannot_advance_ci(self):
        with self.assertRaisesRegex(ValueError, "required_checks"):
            self.driver({**self.manifest, "reviewer": {"required_checks": []}})

    def update(self, **changes):
        key = (self.repo, self.issue)
        self.workflow.snapshots[key] = replace(self.workflow.snapshots[key], **changes)

    def accepted(self, sha=SHA, name="Unit"):
        return ({"name": name, "head_sha": sha, "status": "completed", "conclusion": "success", "id": 1},)

    def failure(self, sha=SHA, conclusion="failure", name="Unit"):
        return ({"name": name, "head_sha": sha, "status": "completed", "conclusion": conclusion, "id": 2},)

    def test_missing_pending_and_stale_sha_checks_wait_for_exact_head(self):
        result = self.driver().advance_once(self.repo, "owner", 100)
        self.assertEqual(result["items"][0]["status"], "WAITING_CI")
        self.update(check_runs=self.accepted(SHA_B))
        result = self.driver().advance_once(self.repo, "owner", 101)
        self.assertEqual(result["items"][0]["status"], "WAITING_CI")
        self.update(check_runs=({"name": "Unit", "head_sha": SHA, "status": "in_progress"},))
        self.assertEqual(self.driver().advance_once(self.repo, "owner", 102)["items"][0]["status"], "WAITING_CI")

    def test_all_exact_head_required_checks_accepted_wait_for_reviewer(self):
        self.update(check_runs=self.accepted())
        result = self.driver().advance_once(self.repo, "owner", 100)
        self.assertEqual(result["items"][0]["status"], "WAITING_REVIEW")

    def test_every_consumer_required_check_must_be_accepted_and_conflicting_failures_are_ambiguous(self):
        manifest = {**self.manifest, "reviewer": {"required_checks": [
            {"name": "Unit", "accepted_conclusions": ["success"],
             "implementation_failure_conclusions": ["failure"]},
            {"name": "Security", "accepted_conclusions": ["success"],
             "infrastructure_failure_conclusions": ["timed_out"]},
        ]}}
        self.update(check_runs=self.accepted())
        self.assertEqual(self.driver(manifest).advance_once(self.repo, "owner", 100)["items"][0]["status"],
                         "WAITING_CI")
        self.update(check_runs=(self.failure()[0],
                                {"name": "Security", "head_sha": SHA, "status": "completed",
                                 "conclusion": "timed_out", "id": 3}))
        result = self.driver(manifest).advance_once(self.repo, "owner", 101)["items"][0]
        self.assertEqual(result["status"], "needs-human")
        self.assertEqual(len(self.implementer.launches), 1)

    def test_explicit_implementation_failure_dispatches_one_core_repair_and_dedupes(self):
        self.update(check_runs=self.failure())
        first = self.driver().advance_once(self.repo, "owner", 100)
        self.assertEqual(first["items"][0]["trigger"], "ci_repair")
        self.assertEqual(first["items"][0]["status"], "launched")
        self.assertEqual(len(self.implementer.launches), 2)
        self.driver().advance_once(self.repo, "owner", 101)
        self.assertEqual(len(self.implementer.launches), 2)

    def test_infrastructure_and_ambiguous_ci_failures_fail_closed(self):
        self.update(check_runs=self.failure(conclusion="timed_out"))
        result = self.driver().advance_once(self.repo, "owner", 100)
        self.assertEqual(result["items"][0]["status"], "infra-blocked")
        self.assertEqual(len(self.implementer.launches), 1)

        self.tearDown(); self.setUp()
        self.update(check_runs=self.failure(conclusion="cancelled"))
        result = self.driver().advance_once(self.repo, "owner", 100)
        self.assertEqual(result["items"][0]["status"], "needs-human")
        self.assertEqual(len(self.implementer.launches), 1)

    def test_exact_head_reviewer_rejection_dispatches_existing_repair(self):
        self.update(check_runs=self.accepted(), coordination_state="changes-requested",
                    formal_review_state="CHANGES_REQUESTED", formal_review_id="review-4",
                    formal_review_head_sha=SHA)
        result = self.driver().advance_once(self.repo, "owner", 100)
        self.assertEqual(result["items"][0]["trigger"], "reviewer_repair")
        self.assertEqual(result["items"][0]["status"], "launched")
        self.assertEqual(len(self.implementer.launches), 2)

    def test_ci_and_reviewer_candidates_share_the_existing_three_repair_budget(self):
        driver = self.driver()
        self.update(check_runs=self.failure(SHA))
        self.assertEqual(driver.advance_once(self.repo, "owner", 100)["items"][0]["status"], "launched")
        self.update(pr_head_sha=SHA_B, check_runs=self.failure(SHA_B))
        self.assertEqual(driver.advance_once(self.repo, "owner", 101)["items"][0]["status"], "launched")
        sha_c = "c" * 40
        self.update(pr_head_sha=sha_c, check_runs=self.accepted(sha_c), coordination_state="changes-requested",
                    formal_review_state="CHANGES_REQUESTED", formal_review_id="review-3",
                    formal_review_head_sha=sha_c)
        self.assertEqual(driver.advance_once(self.repo, "owner", 102)["items"][0]["status"], "launched")
        sha_d = "d" * 40
        self.update(pr_head_sha=sha_d, check_runs=self.failure(sha_d), coordination_state="agent-working",
                    formal_review_state=None, formal_review_id=None, formal_review_head_sha=None)
        result = driver.advance_once(self.repo, "owner", 103)["items"][0]
        self.assertEqual(result["status"], "budget_exhausted")
        self.assertEqual(len(self.implementer.launches), 4)

    def test_stale_review_does_not_authorize_repair_or_approval(self):
        self.update(check_runs=self.accepted(), coordination_state="changes-requested",
                    formal_review_state="CHANGES_REQUESTED", formal_review_id="review-4",
                    formal_review_head_sha=SHA_B)
        self.assertEqual(self.driver().advance_once(self.repo, "owner", 100)["items"][0]["status"],
                         "WAITING_REVIEW")
        self.assertEqual(len(self.implementer.launches), 1)
        self.update(coordination_state="agent-working", formal_review_state="APPROVED",
                    formal_review_head_sha=SHA_B, check_runs=self.accepted())
        self.assertEqual(self.driver().advance_once(self.repo, "owner", 101)["items"][0]["status"],
                         "WAITING_REVIEW")
        self.assertEqual(len(self.implementer.launches), 1)

    def test_exact_approval_waits_for_native_merge_and_head_change_restarts_ci(self):
        self.update(check_runs=self.accepted(), formal_review_state="APPROVED", formal_review_id="review-5",
                    formal_review_head_sha=SHA)
        self.assertEqual(self.driver().advance_once(self.repo, "owner", 100)["items"][0]["status"],
                         "WAITING_MERGE")
        self.update(pr_head_sha=SHA_B, check_runs=self.accepted(SHA_B), formal_review_state="APPROVED",
                    formal_review_head_sha=SHA_B)
        self.assertEqual(self.driver().advance_once(self.repo, "owner", 101)["items"][0]["status"],
                         "WAITING_CI")

    def test_waiting_review_head_change_ignores_old_checks_and_restarts_ci(self):
        self.update(check_runs=self.accepted())
        self.assertEqual(self.driver().advance_once(self.repo, "owner", 100)["items"][0]["status"],
                         "WAITING_REVIEW")
        self.update(pr_head_sha=SHA_B, check_runs=self.accepted(SHA_B), formal_review_state="APPROVED",
                    formal_review_head_sha=SHA_B)
        self.assertEqual(self.driver().advance_once(self.repo, "owner", 101)["items"][0]["status"],
                         "WAITING_CI")

    def test_native_merge_success_precedes_issue_auto_close_cancellation(self):
        self.update(pr_merged=True, pr_state="closed", pr_open=False, issue_open=False,
                    coordination_state="agent-working")
        result = self.driver().advance_once(self.repo, "owner", 100)
        self.assertEqual(result["items"][0]["status"], "merged_success")
        flow = self.core.store.flow(self.repo, self.issue)
        self.assertFalse(flow["trusted"])
        with closing(self.core.store._connect()) as connection:
            self.assertIsNone(connection.execute("SELECT 1 FROM leases WHERE repo=? AND issue_number=?",
                                                 (self.repo, self.issue)).fetchone())
        self.assertIn((self.repo, self.issue), self.workflow.cleared)
        self.assertEqual(self.core.store.attempts_for_repo(self.repo)[0]["phase"], "MERGED_SUCCESS")

    def test_a_different_canonical_merged_pr_cannot_complete_bound_flow(self):
        self.update(pr_number=99, pr_merged=True, pr_state="closed", pr_open=False)
        result = self.driver().advance_once(self.repo, "owner", 100)
        self.assertEqual(result["items"][0]["status"], "unresolved_canonical_pr")
        self.assertTrue(self.core.store.flow(self.repo, self.issue)["trusted"])
        self.assertEqual(self.core.store.attempts_for_repo(self.repo)[0]["phase"], "PR_BOUND")

    def test_untrusted_adopted_flow_can_observe_merge_without_repair_authority(self):
        connection = self.core.store._connect()
        connection.execute("UPDATE flows SET trusted=0,initial_confirmed=0 WHERE repo=? AND issue_number=?",
                           (self.repo, self.issue))
        connection.commit(); connection.close()
        self.update(pr_merged=True, pr_state="closed", pr_open=False, issue_open=False)
        result = self.driver().advance_once(self.repo, "owner", 100)
        self.assertEqual(result["items"][0]["status"], "merged_success")

    def test_closed_unmerged_pr_becomes_needs_human(self):
        self.update(pr_open=False, pr_state="closed", pr_merged=False)
        result = self.driver().advance_once(self.repo, "owner", 100)
        self.assertEqual(result["items"][0]["status"], "needs-human")

    def test_untrusted_adoption_can_observe_but_cannot_repair(self):
        connection = self.core.store._connect()
        connection.execute("UPDATE flows SET trusted=0,initial_confirmed=0 WHERE repo=? AND issue_number=?",
                           (self.repo, self.issue))
        connection.commit(); connection.close()
        self.update(check_runs=self.failure())
        result = self.driver().advance_once(self.repo, "owner", 100)
        self.assertEqual(result["items"][0]["status"], "needs-human")
        self.assertEqual(len(self.implementer.launches), 1)

    def test_ci_deadline_survives_runtime_restart(self):
        driver = self.driver()
        first = driver.advance_once(self.repo, "owner", 100)["items"][0]
        restarted = RuntimeCore(self.db, self.manifest, self.workflow, self.implementer,
                                recovery_timeout_seconds=900)
        result = self.driver(core=restarted).advance_once(self.repo, "owner", first["deadline_at"] + 1)
        self.assertEqual(result["items"][0]["status"], "infra-blocked")

    def test_review_and_merge_deadlines_survive_restart_with_required_terminal_mapping(self):
        self.update(check_runs=self.accepted())
        first = self.driver().advance_once(self.repo, "owner", 100)["items"][0]
        self.assertEqual(first["status"], "WAITING_REVIEW")
        restarted = RuntimeCore(self.db, self.manifest, self.workflow, self.implementer)
        result = self.driver(core=restarted).advance_once(self.repo, "owner", first["deadline_at"] + 1)
        self.assertEqual(result["items"][0]["status"], "needs-human")

        self.tearDown(); self.setUp()
        self.update(check_runs=self.accepted(), formal_review_state="APPROVED", formal_review_head_sha=SHA)
        first = self.driver().advance_once(self.repo, "owner", 100)["items"][0]
        self.assertEqual(first["status"], "WAITING_MERGE")
        restarted = RuntimeCore(self.db, self.manifest, self.workflow, self.implementer)
        result = self.driver(core=restarted).advance_once(self.repo, "owner", first["deadline_at"] + 1)
        self.assertEqual(result["items"][0]["status"], "infra-blocked")

    def test_two_consumers_use_independent_required_check_configs(self):
        second_repo, second_issue = "acme/beta", 8
        self._snapshot(second_repo, second_issue)
        self.assertEqual(self.core.dispatch_initial(second_repo, second_issue, "owner")["status"], "launched")
        second = replace(self.workflow.observe(second_repo, second_issue), coordination_state="agent-working",
                         canonical_relationship_valid=True, canonical_link_count=1, open_linked_pr_count=1,
                         pr_number=88, pr_open=True, pr_state="open", pr_linked_issue=second_issue,
                         pr_head_sha=SHA, pr_branch="implement/8", check_runs=(
                             {"name": "Security", "head_sha": SHA, "status": "completed", "conclusion": "success"},))
        self.workflow.snapshots[(second_repo, second_issue)] = second
        attempt = [row for row in self.core.store.attempts_for_repo(second_repo)
                   if row["issue_number"] == second_issue][0]
        self.core.store.bind_pr(attempt["attempt_id"], 88, "implement/8", SHA, 1)
        self.update(check_runs=self.accepted())
        security_manifest = {**self.manifest, "reviewer": {"required_checks": [
            {"name": "Security", "accepted_conclusions": ["success"]}]}}
        alpha = self.driver().advance_once(self.repo, "owner", 100)
        beta = self.driver(security_manifest).advance_once(second_repo, "owner", 100)
        self.assertEqual(alpha["items"][0]["status"], "WAITING_REVIEW")
        self.assertEqual(beta["items"][0]["status"], "WAITING_REVIEW")


if __name__ == "__main__": unittest.main()
