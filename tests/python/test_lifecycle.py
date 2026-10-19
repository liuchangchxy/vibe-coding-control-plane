import sqlite3
import tempfile
import unittest
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from vccp_runtime.core import (LaunchDisposition, LaunchResult, RepairCandidate, RuntimeCore,
                               WorkflowSnapshot, compute_repair_key)
from vccp_runtime.lifecycle import LifecycleDriver
from vccp_runtime.orchestrator import run_once


SHA = "a" * 40
SHA_B = "b" * 40
PROVIDER = "antigravity"
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
        self.merged = {}
        self.clear_result = True
        # GitHub is eventually consistent: one cycle can observe an Issue that is still open
        # and then, moments later in the same cycle, observe the auto-close that followed the
        # merge. observe_script replays exactly that ordering.
        self.observe_script = []
        self.ready = []

    def observe(self, repo, issue):
        if self.observe_script:
            self.snapshots[(repo, issue)] = self.observe_script.pop(0)
        return self.snapshots[(repo, issue)]

    def discover_ready(self, repo):
        return list(self.ready)

    def merged_closing_pr(self, repo, issue, pr_number):
        return self.merged.get(pr_number)

    def transition_coordination_state(self, repo, issue, expected_state, new_state, expected_revision):
        snapshot = self.observe(repo, issue)
        if snapshot.revision != expected_revision or snapshot.coordination_state != expected_state:
            return False
        terminal = (new_state,) if new_state in {"infra-blocked", "needs-human"} else ()
        self.snapshots[(repo, issue)] = replace(snapshot, revision=snapshot.revision + "+",
            coordination_state=new_state, terminal_labels=terminal)
        self.transitions.append((repo, issue, new_state))
        return True

    def restore_repair_coordination_state(self, repo, issue, new_state, expected_revision):
        return self.transition_coordination_state(repo, issue, "needs-human", new_state, expected_revision)

    def discover_active(self, repo):
        return sorted(issue for (key, issue), snapshot in self.snapshots.items()
                      if key == repo and snapshot.coordination_state in {"agent-working", "changes-requested"})

    def clear_active_coordination_labels(self, repo, issue):
        self.cleared.append((repo, issue))
        snapshot = self.observe(repo, issue)
        self.snapshots[(repo, issue)] = replace(snapshot, active_labels=())
        return self.clear_result


class FakeImplementer:
    def __init__(self): self.launches = []
    def launch(self, request):
        self.launches.append(request)
        return LaunchResult(LaunchDisposition.CONFIRMED, f"exec-{len(self.launches)}", PROVIDER)
    def latest_activity_for(self, provider, execution_id): return None


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

    def driver(self, manifest=None, timeouts=None, core=None, local_reviewer=None):
        return LifecycleDriver(core or self.core, manifest or self.manifest,
                               timeouts or {"waiting_ci": 10, "waiting_review": 10, "waiting_merge": 10},
                               local_reviewer=local_reviewer)

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

    def test_exact_head_accepted_ci_launches_bound_local_reviewer(self):
        class Reviewer:
            def __init__(self): self.calls = []
            def advance(self, repo, issue, pr, head):
                self.calls.append((repo, issue, pr, head))
                return {"status": "reviewer_running"}

        reviewer = Reviewer()
        self.update(check_runs=self.accepted())
        result = self.driver(local_reviewer=reviewer).advance_once(self.repo, "owner", 100)

        self.assertEqual(result["items"][0]["status"], "WAITING_REVIEW")
        self.assertEqual(result["items"][0]["reviewer"]["status"], "reviewer_running")
        self.assertEqual(reviewer.calls, [(self.repo, self.issue, 44, SHA)])

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

    def test_terminalized_budget_flow_recovers_through_run_once_and_restarts_once(self):
        # Reproduce the production database facts: all dispatched work is terminal,
        # trust was cleared, repair_count remains 3, and one exact budget marker exists.
        initial = self.core.store.attempts_for_repo(self.repo)[0]
        sequence = [("implementation_failure", "ci-1"),
                    ("reviewer_rejection", "review-2"),
                    ("implementation_failure", "ci-3")]
        dispatched = [initial]
        for index, (cause, cause_id) in enumerate(sequence, 1):
            candidate = RepairCandidate(self.repo, self.issue, 44, f"{index:040x}",
                                        "implement/7", cause, cause_id, "owner")
            self.workflow.snapshots[(self.repo, self.issue)] = replace(
                self.workflow.snapshots[(self.repo, self.issue)],
                coordination_state="changes-requested" if cause == "reviewer_rejection" else "agent-working",
                active_labels=("changes-requested" if cause == "reviewer_rejection" else "agent-working",),
                pr_head_sha=candidate.head_sha, formal_review_state=("CHANGES_REQUESTED"
                    if cause == "reviewer_rejection" else None), formal_review_id=(cause_id
                    if cause == "reviewer_rejection" else None), formal_review_head_sha=(candidate.head_sha
                    if cause == "reviewer_rejection" else None), revision=f"r{index + 2}")
            result = self.core.dispatch_repair(candidate)
            self.assertEqual(result["status"], "launched")
            self.core.store.bind_pr(result["attempt_id"], 44, "implement/7", candidate.head_sha, 10 + index)
            dispatched.append(self.core.store.attempt(result["attempt_id"]))

        for row in dispatched:
            self.core.store.local_terminal(row["attempt_id"], "STOPPED_CANCELLED", 100)
        with closing(sqlite3.connect(self.db)) as connection:
            connection.execute("UPDATE flows SET repair_count=3,repair_budget_known=1 WHERE repo=? AND issue_number=?",
                               (self.repo, self.issue))
            connection.execute("INSERT INTO controlled_pushes(attempt_id,repo,destination_ref,expected_sha,phase,created_at,completed_at) "
                               "VALUES(?,?,?,?,'completed',1,2)",
                               (initial["attempt_id"], self.repo, "implement/7", SHA))
            connection.execute("INSERT INTO attempts(attempt_id,repo,issue_number,owner_id,lease_token,kind,cause_type,cause_id,"
                               "repair_key,repair_ordinal,expected_head_sha,pr_number,branch,phase,created_at,updated_at) "
                               "VALUES('legacy-budget',?,?,?,'','repair','reviewer_rejection',?,?,4,?,44,?,'BUDGET_EXHAUSTED',3,3)",
                               (self.repo, self.issue, "owner", "review-57",
                                compute_repair_key(self.repo, 44, SHA) + ":budget:legacy-budget", SHA, "implement/7"))
            connection.commit()
        flow = self.core.store.flow(self.repo, self.issue)
        self.assertEqual((flow["trusted"], flow["initial_confirmed"], flow["repair_count"],
                          flow["repair_budget_known"]), (0, 0, 3, 1))

        candidate = RepairCandidate(self.repo, self.issue, 44, SHA, "implement/7",
                                    "reviewer_rejection", "review-57", "owner")
        self.assertFalse(self.core.store.restore_budget_stopped_flow(replace(candidate, head_sha="e" * 40)))
        self.assertEqual(self.core.store.flow(self.repo, self.issue)["trusted"], 0)
        with closing(sqlite3.connect(self.db)) as connection:
            connection.execute("UPDATE attempts SET launch_outcome='unknown' WHERE attempt_id=?",
                               (dispatched[1]["attempt_id"],))
            connection.commit()
        self.assertFalse(self.core.store.restore_budget_stopped_flow(candidate))
        with closing(sqlite3.connect(self.db)) as connection:
            connection.execute("UPDATE attempts SET launch_outcome='confirmed' WHERE attempt_id=?",
                               (dispatched[1]["attempt_id"],))
            connection.commit()
        needs_human = replace(self.workflow.snapshots[(self.repo, self.issue)], revision="r9",
                              coordination_state="needs-human", terminal_labels=("needs-human",),
                              active_labels=(), pr_head_sha=SHA, formal_review_state="CHANGES_REQUESTED",
                              formal_review_id="review-57", formal_review_head_sha=SHA)
        self.workflow.snapshots[(self.repo, self.issue)] = needs_human
        self.assertEqual(self.core.authorize_repair_continuation(candidate, "maintainer")["status"], "authorized")
        restored = self.core.store.flow(self.repo, self.issue)
        self.assertEqual((restored["trusted"], restored["initial_confirmed"], restored["repair_count"],
                          restored["repair_budget_known"]), (1, 1, 3, 1))

        runtime = SimpleNamespace(core=self.core, workflow=self.workflow, lifecycle=self.driver())
        cycle = run_once(runtime, self.repo, "owner", now=200)
        self.assertEqual(len(self.implementer.launches), 5, cycle)  # initial + 3 history + one resumed repair
        created = [row for row in self.core.store.attempts_for_repo(self.repo)
                   if row["kind"] == "repair" and row["phase"] == "LAUNCH_CONFIRMED"]
        self.assertEqual(len(created), 1)
        self.assertEqual((created[0]["issue_number"], created[0]["pr_number"], created[0]["branch"],
                          created[0]["expected_head_sha"], created[0]["repair_ordinal"]),
                         (self.issue, 44, "implement/7", SHA, 4))
        self.assertEqual(len([row for row in self.core.store.attempts_for_repo(self.repo)
                              if row["phase"] == "BUDGET_EXHAUSTED"]), 1)
        self.assertEqual([item.get("trigger") for item in cycle["lifecycle"]["items"]],
                         ["authorized_repair_continuation"])
        restarted_core = RuntimeCore(self.db, self.manifest, self.workflow, self.implementer)
        restarted = run_once(SimpleNamespace(core=restarted_core, workflow=self.workflow,
                                             lifecycle=LifecycleDriver(restarted_core, self.manifest)),
                             self.repo, "owner", now=201)
        self.assertEqual(len(self.implementer.launches), 5)
        self.assertEqual(len([row for row in self.core.store.attempts_for_repo(self.repo)
                              if row["kind"] == "repair" and row["phase"] == "LAUNCH_CONFIRMED"]), 1)
        next_head = "f" * 40
        push_id = self.core.store.begin_controlled_push(created[0]["attempt_id"], self.repo,
                                                        PROVIDER, "implement/7", next_head, 202)
        self.assertIsNotNone(push_id)
        self.assertTrue(self.core.store.complete_controlled_push(push_id, next_head, 203))
        self.assertTrue(self.core.store.bind_pr(created[0]["attempt_id"], 44, "implement/7", next_head, 204))
        self.workflow.snapshots[(self.repo, self.issue)] = replace(
            self.workflow.snapshots[(self.repo, self.issue)], revision="r10",
            coordination_state="agent-working", terminal_labels=(), active_labels=("agent-working",),
            pr_head_sha=next_head, formal_review_state=None, formal_review_id=None,
            formal_review_head_sha=None,
            check_runs=({"name": "Unit", "id": 90, "head_sha": next_head,
                         "status": "completed", "conclusion": "success"},))
        ci = run_once(runtime, self.repo, "owner", now=205)
        self.assertEqual(ci["lifecycle"]["items"][0]["status"], "WAITING_REVIEW")
        self.workflow.snapshots[(self.repo, self.issue)] = replace(
            self.workflow.snapshots[(self.repo, self.issue)], revision="r11",
            formal_review_state="APPROVED", formal_review_id="review-approved",
            formal_review_head_sha=next_head)
        approved = run_once(runtime, self.repo, "owner", now=206)
        self.assertEqual(approved["lifecycle"]["items"][0]["status"], "WAITING_MERGE")
        self.workflow.snapshots[(self.repo, self.issue)] = replace(
            self.workflow.snapshots[(self.repo, self.issue)], issue_open=False, pr_merged=True,
            revision="r12")
        merged = run_once(runtime, self.repo, "owner", now=207)
        self.assertEqual(merged["lifecycle"]["items"][0]["status"], "merged_success")
        self.assertEqual(self.core.store.lifecycle_state(self.repo, self.issue)["phase"], "MERGED_SUCCESS")
    def test_ci_repair_in_flight_suppresses_old_head_lifecycle_without_deadline_change(self):
        driver = self.driver()
        self.assertEqual(driver.advance_once(self.repo, "owner", 100)["items"][0]["status"], "WAITING_CI")
        self.update(check_runs=self.failure())
        self.assertEqual(driver.advance_once(self.repo, "owner", 101)["items"][0]["status"], "launched")
        prior = self.core.store.lifecycle_state(self.repo, self.issue)
        result = driver.advance_once(self.repo, "owner", 102)
        self.assertEqual(result["items"][0]["status"], "waiting_for_repair")
        self.assertEqual(len(self.implementer.launches), 2)
        after = self.core.store.lifecycle_state(self.repo, self.issue)
        self.assertEqual((after["phase"], after["deadline_at"]), (prior["phase"], prior["deadline_at"]))

    def test_reviewer_repair_in_flight_does_not_enter_or_reset_review_wait(self):
        driver = self.driver()
        self.update(check_runs=self.accepted())
        first = driver.advance_once(self.repo, "owner", 100)["items"][0]
        self.assertEqual(first["status"], "WAITING_REVIEW")
        review_deadline = first["deadline_at"]
        self.update(coordination_state="changes-requested", formal_review_state="CHANGES_REQUESTED",
                    formal_review_id="review-6", formal_review_head_sha=SHA)
        self.assertEqual(driver.advance_once(self.repo, "owner", 101)["items"][0]["status"], "launched")
        result = driver.advance_once(self.repo, "owner", 102)
        self.assertEqual(result["items"][0]["status"], "waiting_for_repair")
        self.assertNotEqual(result["items"][0]["status"], "WAITING_REVIEW")
        self.assertEqual(len(self.implementer.launches), 2)
        state = self.core.store.lifecycle_state(self.repo, self.issue)
        self.assertEqual((state["phase"], state["deadline_at"]), ("WAITING_REVIEW", review_deadline))

    def test_repair_push_binds_same_pr_branch_then_starts_ci_for_new_head_only(self):
        driver = self.driver()
        self.update(check_runs=self.failure(SHA))
        self.assertEqual(driver.advance_once(self.repo, "owner", 100)["items"][0]["status"], "launched")
        self.update(pr_head_sha=SHA_B, check_runs=self.accepted(SHA),
                    formal_review_state="APPROVED", formal_review_head_sha=SHA)
        result = driver.advance_once(self.repo, "owner", 101)
        self.assertEqual(result["items"][0]["status"], "WAITING_CI")
        attempts = self.core.store.attempts_for_repo(self.repo)
        repair = [row for row in attempts if row["kind"] == "repair"][0]
        self.assertEqual((repair["phase"], repair["pr_number"], repair["branch"]),
                         ("PR_BOUND", 44, "implement/7"))
        self.assertEqual(repair["expected_head_sha"], SHA)
        self.assertEqual(repair["resulting_head_sha"], SHA_B)
        lifecycle = self.core.store.lifecycle_state(self.repo, self.issue)
        self.assertEqual((lifecycle["phase"], lifecycle["head_sha"]), ("WAITING_CI", SHA_B))
        self.assertEqual(len(self.implementer.launches), 2)
        self.update(check_runs=self.accepted(SHA_B), formal_review_state="APPROVED",
                    formal_review_head_sha=SHA)
        self.assertEqual(driver.advance_once(self.repo, "owner", 102)["items"][0]["status"],
                         "WAITING_REVIEW")

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

    def test_ci_and_reviewer_candidates_use_classified_caps_and_global_boundary(self):
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
        self.assertEqual(result["status"], "launched")
        self.assertEqual(len(self.implementer.launches), 5)

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
        self.assertEqual(self.core.store.lifecycle_state(self.repo, self.issue)["outcome"], "merged")

    def test_cleanup_that_did_not_land_is_reported_as_pending(self):
        self.update(pr_merged=True, pr_state="closed", pr_open=False, issue_open=False,
                    coordination_state="agent-working")
        self.workflow.clear_result = False
        self.assertEqual(self.driver().advance_once(self.repo, "owner", 100)["items"][0]["status"],
                         "merged_success")
        state = self.core.store.lifecycle_state(self.repo, self.issue)
        self.assertEqual(state["phase"], "MERGED_SUCCESS")
        self.assertEqual(state["outcome"], "merged_cleanup_pending")
        self.assertFalse(self.driver().advance_once(self.repo, "owner", 101)["items"][0]
                         ["active_labels_cleared"])

    def test_issue_auto_close_that_hides_the_closing_link_still_records_merge(self):
        # GitHub clears willCloseTarget the moment the merge auto-closes the Issue, so the
        # snapshot loses the canonical PR entirely before the next poll can read it.
        self.update(pr_number=None, pr_open=False, pr_state="closed", pr_merged=False,
                    canonical_link_count=0, open_linked_pr_count=0, issue_open=False,
                    coordination_state="invalid", active_labels=())
        self.core.store.set_lifecycle(self.repo, self.issue, "WAITING_MERGE", SHA, 100, 10)
        self.workflow.merged[44] = {"headRefName": "implement/7", "headRefOid": SHA}
        result = self.driver().advance_once(self.repo, "owner", 100)
        self.assertEqual(result["items"][0]["status"], "merged_success")
        self.assertEqual(result["items"][0]["head"], SHA)
        self.assertIn((self.repo, self.issue), self.workflow.cleared)
        self.assertEqual(self.core.store.attempts_for_repo(self.repo)[0]["phase"], "MERGED_SUCCESS")
        self.assertEqual(self.core.store.lifecycle_state(self.repo, self.issue)["phase"], "MERGED_SUCCESS")
        self.assertEqual(self.core.store.lifecycle_state(self.repo, self.issue)["outcome"], "merged")

    def test_hidden_link_for_a_different_branch_cannot_complete_the_bound_flow(self):
        self.update(pr_number=None, pr_open=False, pr_state="closed", pr_merged=False,
                    canonical_link_count=0, open_linked_pr_count=0, issue_open=False,
                    coordination_state="invalid", active_labels=())
        self.core.store.set_lifecycle(self.repo, self.issue, "WAITING_MERGE", SHA, 100, 10)
        self.workflow.merged[44] = {"headRefName": "someone-elses/branch", "headRefOid": SHA}
        result = self.driver().advance_once(self.repo, "owner", 100)
        self.assertEqual(result["items"][0]["status"], "unresolved_canonical_pr")
        self.assertNotIn((self.repo, self.issue), self.workflow.cleared)
        self.assertEqual(self.core.store.lifecycle_state(self.repo, self.issue)["phase"], "WAITING_MERGE")
        self.assertEqual(self.core.store.attempts_for_repo(self.repo)[0]["phase"], "PR_BOUND")

    def test_hidden_link_needs_the_authorized_waiting_merge_head(self):
        # A merged PR alone is not authorization: the head it reports must be the exact
        # head the durable WAITING_MERGE already holds, i.e. the head the required checks
        # and the formal APPROVE were observed at.
        self.update(pr_number=None, pr_open=False, pr_state="closed", pr_merged=False,
                    canonical_link_count=0, open_linked_pr_count=0, issue_open=False,
                    coordination_state="invalid", active_labels=())
        self.workflow.merged[44] = {"headRefName": "implement/7", "headRefOid": SHA}
        for phase, head in ((None, None), ("WAITING_REVIEW", SHA), ("WAITING_CI", SHA),
                            ("WAITING_MERGE", SHA_B), ("WAITING_MERGE", "")):
            with closing(self.core.store._connect()) as connection:
                connection.execute("DELETE FROM lifecycle WHERE repo=? AND issue_number=?",
                                   (self.repo, self.issue))
                connection.commit()
            if phase is not None:
                self.core.store.set_lifecycle(self.repo, self.issue, phase, head, 100, 10)
            result = self.driver().advance_once(self.repo, "owner", 100)
            self.assertEqual(result["items"][0]["status"], "unresolved_canonical_pr", (phase, head))
            self.assertNotIn((self.repo, self.issue), self.workflow.cleared)
            self.assertEqual(self.core.store.attempts_for_repo(self.repo)[0]["phase"], "PR_BOUND")

    # --- proven merge must outrank a cancellation fence (VCCP #32) ---------------------

    def hidden_closing_link_snapshot(self, **changes):
        """The GitHub shape right after a native merge auto-closed the Issue.

        The durable attempt and its bound PR survive, but the Issue timeline stops reporting
        ``willCloseTarget``, so the snapshot loses the canonical PR (and, on a merged PR, the
        formal review, because reviews are only read while the PR is open).
        """
        fields = dict(pr_number=None, pr_open=False, pr_state="closed", pr_merged=False,
                      canonical_link_count=0, open_linked_pr_count=0, issue_open=False,
                      coordination_state="invalid", active_labels=())
        fields.update(changes)
        return replace(self.workflow.snapshots[(self.repo, self.issue)], **fields)

    def runtime(self, **overrides):
        parts = {"core": self.core, "workflow": self.workflow, "lifecycle": self.driver(),
                 "implementer": self.implementer}
        parts.update(overrides)
        return SimpleNamespace(**parts)

    def fence_by_reconcile(self, snapshot=None):
        """Fence the bound attempt exactly as production did, and prove it took effect."""
        self.workflow.snapshots[(self.repo, self.issue)] = (
            snapshot if snapshot is not None else self.hidden_closing_link_snapshot())
        self.core.reconcile_once(self.repo, "owner", 100)
        attempt = self.core.store.attempts_for_repo(self.repo)[0]
        self.assertEqual(attempt["phase"], "STOPPED_CANCELLED")
        return attempt

    def assert_not_recovered(self):
        self.assertEqual(self.core.store.attempts_for_repo(self.repo)[0]["phase"],
                         "STOPPED_CANCELLED")
        self.assertEqual(self.core.store.lifecycle_state(self.repo, self.issue)["phase"],
                         "WAITING_MERGE")
        self.assertNotIn((self.repo, self.issue), self.workflow.cleared)
        self.assertEqual(len(self.implementer.launches), 1)

    def test_merge_missed_by_the_lifecycle_pass_recovers_after_the_cancellation_fence(self):
        # Production sequence, driven step by step so the fence itself is observable.
        # run_once() orders a cycle as advance_once -> reconcile_once -> ... -> advance_once,
        # so in production the recovery can land in the same cycle's second lifecycle pass;
        # here the pieces are called directly to show the fenced state in between.
        self.core.store.set_lifecycle(self.repo, self.issue, "WAITING_MERGE", SHA, 100, 10)
        self.workflow.merged[44] = {"headRefName": "implement/7", "headRefOid": SHA}
        stale = self.workflow.snapshots[(self.repo, self.issue)]
        closed = self.hidden_closing_link_snapshot()
        self.workflow.observe_script = [stale, closed]

        # 1. The lifecycle pass still holds a pre-merge view and misses the merge.
        missed = self.driver().advance_once(self.repo, "owner", 100)
        self.assertNotIn("merged_success", [item.get("status") for item in missed["items"]])
        self.assertEqual(self.core.store.attempts_for_repo(self.repo)[0]["phase"], "PR_BOUND")

        # 2. reconcile observes the auto-close and fences the bound attempt.
        self.workflow.observe_script = [closed, closed]
        self.core.reconcile_once(self.repo, "owner", 100)
        self.assertEqual(self.core.store.attempts_for_repo(self.repo)[0]["phase"],
                         "STOPPED_CANCELLED")
        self.assertEqual(self.core.store.lifecycle_state(self.repo, self.issue)["phase"],
                         "WAITING_MERGE")

        # 3. A later lifecycle observation must still reach the durable exact-head proof.
        cycle = run_once(self.runtime(), self.repo, "owner", 101)
        self.assertIn("merged_success",
                      [item.get("status") for item in cycle["lifecycle"]["items"]])
        self.assertEqual(self.core.store.attempts_for_repo(self.repo)[0]["phase"],
                         "MERGED_SUCCESS")
        state = self.core.store.lifecycle_state(self.repo, self.issue)
        self.assertEqual(state["phase"], "MERGED_SUCCESS")
        self.assertEqual(state["outcome"], "merged")
        self.assertFalse(self.core.store.flow(self.repo, self.issue)["trusted"])
        with closing(self.core.store._connect()) as connection:
            self.assertIsNone(connection.execute(
                "SELECT 1 FROM leases WHERE repo=? AND issue_number=?",
                (self.repo, self.issue)).fetchone())
        self.assertIn((self.repo, self.issue), self.workflow.cleared)
        self.assertEqual(len(self.implementer.launches), 1)

        # 4. Recovery is idempotent: another cycle never completes the flow a second time.
        again = run_once(self.runtime(), self.repo, "owner", 102)
        self.assertEqual(self.core.store.attempts_for_repo(self.repo)[0]["phase"],
                         "MERGED_SUCCESS")
        self.assertEqual(self.core.store.lifecycle_state(self.repo, self.issue)["outcome"], "merged")
        self.assertNotIn("merged_success", [item.get("status") for item in again["lifecycle"]["items"]])
        self.assertEqual(len(self.workflow.cleared), 1)

    def test_fenced_attempt_without_merged_closing_evidence_does_not_recover(self):
        self.core.store.set_lifecycle(self.repo, self.issue, "WAITING_MERGE", SHA, 100, 10)
        self.fence_by_reconcile()
        self.driver().advance_once(self.repo, "owner", 101)
        self.assert_not_recovered()

    def test_fenced_attempt_with_a_different_merged_head_does_not_recover(self):
        self.core.store.set_lifecycle(self.repo, self.issue, "WAITING_MERGE", SHA, 100, 10)
        self.workflow.merged[44] = {"headRefName": "implement/7", "headRefOid": SHA_B}
        self.fence_by_reconcile()
        self.driver().advance_once(self.repo, "owner", 101)
        self.assert_not_recovered()

    def test_fenced_attempt_with_a_different_branch_does_not_recover(self):
        self.core.store.set_lifecycle(self.repo, self.issue, "WAITING_MERGE", SHA, 100, 10)
        self.workflow.merged[44] = {"headRefName": "someone-elses/branch", "headRefOid": SHA}
        self.fence_by_reconcile()
        self.driver().advance_once(self.repo, "owner", 101)
        self.assert_not_recovered()

    def test_fenced_attempt_without_a_durable_waiting_merge_head_does_not_recover(self):
        self.workflow.merged[44] = {"headRefName": "implement/7", "headRefOid": SHA}
        for phase, head in (("WAITING_REVIEW", SHA), ("WAITING_MERGE", SHA_B), ("WAITING_MERGE", "")):
            with self.subTest(phase=phase, head=head):
                for attempt_id in [row["attempt_id"]
                                   for row in self.core.store.attempts_for_repo(self.repo)]:
                    self.core.store.local_terminal(attempt_id, "STOPPED_CANCELLED", 100)
                self.core.store.set_lifecycle(self.repo, self.issue, phase, head, 100, 10)
                result = self.driver().advance_once(self.repo, "owner", 101)
                self.assertNotIn("merged_success",
                                 [item.get("status") for item in result["items"]])
                self.assertEqual(self.core.store.attempts_for_repo(self.repo)[0]["phase"],
                                 "STOPPED_CANCELLED")

    def test_manually_closed_issue_never_recovers_a_fenced_attempt(self):
        # A human closing the Issue is not a merge, with or without a stale WAITING_MERGE.
        for phase in ("WAITING_MERGE", None):
            with self.subTest(lifecycle=phase):
                for attempt_id in [row["attempt_id"]
                                   for row in self.core.store.attempts_for_repo(self.repo)]:
                    self.core.store.local_terminal(attempt_id, "STOPPED_CANCELLED", 100)
                with closing(self.core.store._connect()) as connection:
                    connection.execute("DELETE FROM lifecycle WHERE repo=? AND issue_number=?",
                                       (self.repo, self.issue))
                    connection.commit()
                if phase is not None:
                    self.core.store.set_lifecycle(self.repo, self.issue, phase, SHA, 100, 10)
                self.workflow.snapshots[(self.repo, self.issue)] = \
                    self.hidden_closing_link_snapshot(coordination_state="agent-working")
                result = self.driver().advance_once(self.repo, "owner", 101)
                self.assertNotIn("merged_success",
                                 [item.get("status") for item in result["items"]])
                self.assertEqual(self.core.store.attempts_for_repo(self.repo)[0]["phase"],
                                 "STOPPED_CANCELLED")
                self.assertNotIn((self.repo, self.issue), self.workflow.cleared)

    def test_fenced_attempt_whose_issue_is_still_open_is_never_recovered(self):
        self.core.store.set_lifecycle(self.repo, self.issue, "WAITING_MERGE", SHA, 100, 10)
        self.workflow.merged[44] = {"headRefName": "implement/7", "headRefOid": SHA}
        attempt_id = self.core.store.attempts_for_repo(self.repo)[0]["attempt_id"]
        self.core.store.local_terminal(attempt_id, "STOPPED_CANCELLED", 100)
        self.update(issue_open=True, coordination_state="agent-working", pr_merged=False)
        result = self.driver().advance_once(self.repo, "owner", 101)
        self.assertNotIn("merged_success", [item.get("status") for item in result["items"]])
        self.assertEqual(self.core.store.attempts_for_repo(self.repo)[0]["phase"],
                         "STOPPED_CANCELLED")
        self.assertEqual(self.core.store.lifecycle_state(self.repo, self.issue)["phase"],
                         "WAITING_MERGE")

    def test_unbound_cancelled_attempt_never_enters_merge_observation(self):
        # The fence also covers attempts with no bound PR/branch; those must never be
        # re-offered, and neither may an attempt that is not fenced at all.
        driver = self.driver()
        self.core.store.set_lifecycle(self.repo, self.issue, "WAITING_MERGE", SHA, 100, 10)
        fenced = {"phase": "STOPPED_CANCELLED", "issue_number": self.issue,
                  "pr_number": None, "branch": None}
        self.assertFalse(driver._merge_observation_after_fence(self.repo, fenced))
        self.assertFalse(driver._merge_observation_after_fence(
            self.repo, {**fenced, "pr_number": 44, "branch": None}))
        self.assertFalse(driver._merge_observation_after_fence(
            self.repo, {**fenced, "pr_number": None, "branch": "implement/7"}))
        self.assertTrue(driver._merge_observation_after_fence(
            self.repo, {**fenced, "pr_number": 44, "branch": "implement/7"}))
        self.assertFalse(driver._merge_observation_after_fence(
            self.repo, {**fenced, "phase": "PR_BOUND", "pr_number": 44,
                        "branch": "implement/7"}))

    def add_bound_attempt(self, attempt_id, pr_number, branch, phase="STOPPED_CANCELLED"):
        """Add a second durable attempt for the same Issue, as a repaired flow can hold."""
        with closing(self.core.store._connect()) as connection:
            connection.execute(
                "INSERT INTO attempts(attempt_id,repo,issue_number,owner_id,lease_token,kind,"
                "pr_number,branch,phase,created_at,updated_at,phase_entered_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (attempt_id, self.repo, self.issue, "owner", "", "initial_dispatch",
                 pr_number, branch, phase, 100, 100, 100))
            connection.commit()

    def phases(self):
        return {row["attempt_id"]: row["phase"]
                for row in self.core.store.attempts_for_repo(self.repo)}

    def test_only_the_stopped_attempt_the_proof_establishes_is_recovered(self):
        # Two fenced attempts on one Issue: only the one whose own bound PR/branch the
        # exact-head proof established may be lifted.
        self.core.store.set_lifecycle(self.repo, self.issue, "WAITING_MERGE", SHA, 100, 10)
        self.workflow.merged[44] = {"headRefName": "implement/7", "headRefOid": SHA}
        self.workflow.snapshots[(self.repo, self.issue)] = self.hidden_closing_link_snapshot()
        self.add_bound_attempt("attempt-other-pr", 55, "other/7")
        proven = next(row["attempt_id"] for row in self.core.store.attempts_for_repo(self.repo)
                      if row["pr_number"] == 44)
        for row in self.core.store.attempts_for_repo(self.repo):
            self.core.store.local_terminal(row["attempt_id"], "STOPPED_CANCELLED", 100)

        self.driver().advance_once(self.repo, "owner", 101)

        self.assertEqual(self.phases(),
                         {proven: "MERGED_SUCCESS", "attempt-other-pr": "STOPPED_CANCELLED"})
        self.assertEqual(self.core.store.lifecycle_state(self.repo, self.issue)["outcome"], "merged")

    def test_stopped_attempt_bound_to_a_different_pr_is_not_recovered(self):
        # The Issue's closing PR is #44. A fenced attempt bound to another PR whose
        # merged_closing_pr() proves nothing must stay fenced.
        self.core.store.set_lifecycle(self.repo, self.issue, "WAITING_MERGE", SHA, 100, 10)
        self.workflow.merged[44] = {"headRefName": "implement/7", "headRefOid": SHA}
        self.workflow.snapshots[(self.repo, self.issue)] = self.hidden_closing_link_snapshot()
        self.add_bound_attempt("attempt-other-pr", 55, "other/7")
        proven = next(row["attempt_id"] for row in self.core.store.attempts_for_repo(self.repo)
                      if row["pr_number"] == 44)

        self.driver().advance_once(self.repo, "owner", 101)

        self.assertEqual(self.phases(),
                         {proven: "MERGED_SUCCESS", "attempt-other-pr": "STOPPED_CANCELLED"})

    def test_stopped_attempt_without_a_bound_branch_is_not_recovered(self):
        # The Issue is still observed (the original attempt is bound normally), so the only
        # thing keeping the extra row fenced is that it records no bound branch.
        self.core.store.set_lifecycle(self.repo, self.issue, "WAITING_MERGE", SHA, 100, 10)
        self.workflow.merged[44] = {"headRefName": "implement/7", "headRefOid": SHA}
        self.workflow.snapshots[(self.repo, self.issue)] = self.hidden_closing_link_snapshot()
        for branch in (None, ""):
            with self.subTest(branch=branch):
                self.add_bound_attempt(f"attempt-nobranch-{branch!r}", 55, branch)
                self.driver().advance_once(self.repo, "owner", 101)
                self.assertEqual(self.phases()[f"attempt-nobranch-{branch!r}"],
                                 "STOPPED_CANCELLED")

    def test_stopped_attempt_with_a_mismatched_branch_is_not_recovered(self):
        self.core.store.set_lifecycle(self.repo, self.issue, "WAITING_MERGE", SHA, 100, 10)
        # Row 55's branch does not match the branch the merged closing PR reports.
        self.workflow.merged[55] = {"headRefName": "implement/7", "headRefOid": SHA}
        self.workflow.snapshots[(self.repo, self.issue)] = self.hidden_closing_link_snapshot()
        self.add_bound_attempt("attempt-mismatch", 55, "other/7")

        self.driver().advance_once(self.repo, "owner", 101)

        self.assertEqual(self.phases()["attempt-mismatch"], "STOPPED_CANCELLED")
        self.assertEqual(self.core.store.lifecycle_state(self.repo, self.issue)["phase"],
                         "WAITING_MERGE")

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

    def test_restart_adoption_without_budget_evidence_blocks_ci_repair(self):
        connection = self.core.store._connect()
        connection.execute("DELETE FROM attempts WHERE repo=? AND issue_number=?", (self.repo, self.issue))
        connection.execute("DELETE FROM flows WHERE repo=? AND issue_number=?", (self.repo, self.issue))
        connection.commit(); connection.close()
        adopted = self.core.reconcile_once(self.repo, "owner", 100)
        self.assertEqual(adopted["items"][0]["status"], "orphan_adopted_trusted")
        self.update(check_runs=self.failure())
        repaired = self.driver().advance_once(self.repo, "owner", 101)
        self.assertEqual(repaired["items"][0]["status"], "needs-human")
        self.assertEqual(repaired["items"][0]["outcome"],
                         "repair_history_or_budget_provenance_unknown")
        repair = [row for row in self.core.store.attempts_for_repo(self.repo) if row["kind"] == "repair"]
        self.assertEqual(repair, [])
        self.assertEqual(len(self.implementer.launches), 1)

    def test_adopted_pr_with_unknown_budget_remains_observable_through_merge(self):
        connection = self.core.store._connect()
        connection.execute("DELETE FROM attempts WHERE repo=? AND issue_number=?", (self.repo, self.issue))
        connection.execute("DELETE FROM flows WHERE repo=? AND issue_number=?", (self.repo, self.issue))
        connection.commit(); connection.close()
        self.core.reconcile_once(self.repo, "owner", 100)
        self.update(check_runs=self.accepted())
        self.assertEqual(self.driver().advance_once(self.repo, "owner", 101)["items"][0]["status"],
                         "WAITING_REVIEW")
        self.update(formal_review_state="APPROVED", formal_review_head_sha=SHA)
        self.assertEqual(self.driver().advance_once(self.repo, "owner", 102)["items"][0]["status"],
                         "WAITING_MERGE")
        self.update(pr_merged=True, pr_state="closed", pr_open=False, issue_open=False)
        self.assertEqual(self.driver().advance_once(self.repo, "owner", 103)["items"][0]["status"],
                         "merged_success")
        self.assertEqual(len([row for row in self.core.store.attempts_for_repo(self.repo)
                              if row["kind"] == "repair"]), 0)

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
