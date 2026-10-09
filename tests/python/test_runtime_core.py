import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

from vccp_runtime import (
    LaunchDisposition,
    LaunchResult,
    RepairCandidate,
    RuntimeCore,
    WorkflowSnapshot,
    compute_repair_key,
)


REVISION = "a" * 40
BRANCH = "implement/issue-7"
MANIFEST_V2 = {
    "schema_version": 2,
    "issue_contract": {"max_automated_repairs": 3},
}


def snapshot(repo="acme/alpha", issue=7, state="agent-ready", revision=1):
    return WorkflowSnapshot(
        repo=repo,
        issue_number=issue,
        revision=str(revision),
        issue_open=True,
        coordination_state=state,
        frozen_spec=True,
    )


def repair_snapshot(candidate, state=None, revision=20):
    return WorkflowSnapshot(
        repo=candidate.repo,
        issue_number=candidate.issue_number,
        revision=str(revision),
        issue_open=True,
        coordination_state=state or ("changes-requested" if candidate.cause_type == "reviewer_rejection" else "agent-working"),
        frozen_spec=True,
        pr_number=candidate.pr_number,
        pr_open=True,
        pr_linked_issue=candidate.issue_number,
        pr_head_sha=candidate.head_sha,
        pr_branch=candidate.branch,
        formal_review_state="CHANGES_REQUESTED" if candidate.cause_type == "reviewer_rejection" else None,
        formal_review_id=candidate.cause_id if candidate.cause_type == "reviewer_rejection" else None,
        formal_review_head_sha=candidate.head_sha if candidate.cause_type == "reviewer_rejection" else None,
    )


class FakeWorkflow:
    def __init__(self, initial, static_transition=False):
        self.current = initial
        self.static_transition = static_transition
        self.transitions = []
        self.fail_transition = False
        self.observe_barrier = None
        self._barrier_threads = set()
        self._lock = threading.Lock()

    def observe(self, repo, issue_number):
        barrier = None
        with self._lock:
            if self.observe_barrier is not None and threading.get_ident() not in self._barrier_threads:
                self._barrier_threads.add(threading.get_ident())
                barrier = self.observe_barrier
        if barrier is not None:
            barrier.wait(timeout=5)
        with self._lock:
            if self.current.repo.casefold() != repo.casefold() or self.current.issue_number != issue_number:
                raise AssertionError("unexpected workflow identity")
            return self.current

    def set_snapshot(self, value):
        with self._lock:
            self.current = value

    def transition_coordination_state(self, repo, issue_number, expected_state, new_state, expected_revision):
        with self._lock:
            self.transitions.append((repo, issue_number, expected_state, new_state, expected_revision))
            if self.fail_transition:
                return False
            if self.current.coordination_state != expected_state or self.current.revision != expected_revision:
                return False
            if not self.static_transition:
                self.current = replace(
                    self.current,
                    coordination_state=new_state,
                    revision=f"{int(self.current.revision) + 1}",
                )
            return True


class FakeImplementer:
    def __init__(self, results=None):
        self.results = list(results or [])
        self.requests = []
        self.activity = None
        self._lock = threading.Lock()

    def launch(self, request):
        with self._lock:
            self.requests.append(request)
            if self.results:
                return self.results.pop(0)
        return LaunchResult(LaunchDisposition.CONFIRMED, f"execution-{len(self.requests)}")

    def latest_activity(self, conversation_id):
        return self.activity


class ThreadRoutedWorkflow:
    """Return independent exact-head observations to workers racing in one test."""

    def __init__(self, snapshots_by_thread, barrier):
        self.snapshots_by_thread = snapshots_by_thread
        self.barrier = barrier
        self.seen = set()
        self.transitions = []
        self._lock = threading.Lock()

    def observe(self, repo, issue_number):
        name = threading.current_thread().name
        with self._lock:
            first = name not in self.seen
            self.seen.add(name)
        if first:
            self.barrier.wait(timeout=5)
        value = self.snapshots_by_thread[name]
        if value.repo.casefold() != repo.casefold() or value.issue_number != issue_number:
            raise AssertionError("unexpected workflow identity")
        return value

    def transition_coordination_state(self, repo, issue_number, expected_state, new_state, expected_revision):
        self.transitions.append((repo, issue_number, expected_state, new_state, expected_revision))
        return True


class RuntimeCoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "runtime.sqlite3"
        self.workflow = FakeWorkflow(snapshot())
        self.implementer = FakeImplementer()
        self.core = RuntimeCore(self.db, MANIFEST_V2, self.workflow, self.implementer)

    def tearDown(self):
        self.temp.cleanup()

    def dispatch_initial(self, repo="acme/alpha", issue=7, owner="worker"):
        return self.core.dispatch_initial(repo, issue, owner)

    def establish_flow(self, repo="acme/alpha", issue=7, owner="worker"):
        result = self.core.dispatch_initial(repo, issue, owner)
        self.assertEqual(result["status"], "launched")
        return result

    def candidate(self, sha=REVISION, cause="implementation_failure", cause_id="check-1", repo="acme/alpha", issue=7):
        return RepairCandidate(repo, issue, 44, sha, BRANCH, cause, cause_id, "worker")

    def prepare_repair(self, candidate):
        self.workflow.set_snapshot(repair_snapshot(candidate))

    def run_mixed_repairs(self):
        self.establish_flow()
        causes = [
            ("implementation_failure", "check-1"),
            ("reviewer_rejection", "review-2"),
            ("implementation_failure", "check-3"),
        ]
        results = []
        for ordinal, (cause, cause_id) in enumerate(causes, start=1):
            state = "changes-requested" if cause == "reviewer_rejection" else "agent-working"
            candidate = self.candidate(sha=f"{ordinal:040x}", cause=cause, cause_id=cause_id)
            self.workflow.set_snapshot(repair_snapshot(candidate, state=state, revision=20 + ordinal))
            results.append(self.core.dispatch_repair(candidate))
        return results

    def test_initial_dispatch_persists_confirmed_lifecycle_and_execution_identity(self):
        result = self.dispatch_initial()
        self.assertEqual((result["status"], result["phase"]), ("launched", "LAUNCH_CONFIRMED"))
        self.assertEqual(result["execution_id"], "execution-1")
        attempt = self.core.store.attempt(result["attempt_id"])
        self.assertEqual(attempt["phase"], "LAUNCH_CONFIRMED")
        self.assertEqual(attempt["launch_outcome"], "confirmed")
        self.assertEqual(attempt["execution_id"], "execution-1")
        reopened = RuntimeCore(self.db, MANIFEST_V2, self.workflow, self.implementer)
        self.assertEqual(reopened.store.attempt(result["attempt_id"])["execution_id"], "execution-1")
        self.assertEqual(len(self.workflow.transitions), 1)
        self.assertEqual(
            [entry[2:4] for entry in self.workflow.transitions],
            [("agent-ready", "agent-working")],
        )

    def test_conversation_activity_advances_durable_deadline_and_stall_is_terminal(self):
        result = self.dispatch_initial()
        row = self.core.store.attempt(result["attempt_id"])
        baseline = row["implementer_activity_at"]
        self.implementer.activity = baseline  # Existing static artifact is not progress.
        self.assertEqual(self.core.observe_implementer_progress("acme/alpha", baseline + 100)
                         ["items"][0]["status"], "no_new_activity")
        self.implementer.activity = baseline + 20
        self.assertEqual(self.core.observe_implementer_progress("acme/alpha", baseline + 21)
                         ["items"][0]["status"], "progress")
        refreshed = self.core.store.attempt(result["attempt_id"])
        self.assertEqual(refreshed["implementer_activity_at"], baseline + 20)
        self.core.store.renew_owner_lease("acme/alpha", 7, "worker", baseline + 30, 300)
        self.assertEqual(self.core.store.attempt(result["attempt_id"])["implementer_activity_at"], baseline + 20)
        outcome = self.core.observe_implementer_progress("acme/alpha", baseline + 20 + 1800)
        self.assertEqual(outcome["items"][0]["status"], "implementer_stalled")
        self.assertEqual(self.workflow.current.coordination_state, "infra-blocked")

    def test_two_worker_initial_claim_race_launches_once(self):
        other = RuntimeCore(self.db, MANIFEST_V2, self.workflow, self.implementer)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(
                lambda args: args[0].dispatch_initial("acme/alpha", 7, args[1]),
                [(self.core, "worker-a"), (other, "worker-b")],
            ))
        self.assertEqual(len(self.implementer.requests), 1)
        self.assertEqual(sum(result["status"] == "launched" for result in results), 1)
        self.assertTrue(any(result["status"] in {"lease_held", "ineligible"} for result in results))

    def test_failed_coordination_claim_records_no_launch(self):
        self.workflow.fail_transition = True
        result = self.dispatch_initial()
        self.assertEqual(result["status"], "claim_failed")
        self.assertEqual(self.implementer.requests, [])
        self.assertEqual(self.core.store.attempt(result["attempt_id"])["phase"], "CLAIM_FAILED")

    def test_initial_dispatch_does_not_adopt_an_existing_linked_pr(self):
        self.workflow.set_snapshot(replace(snapshot(), open_linked_pr_count=1))
        result = self.dispatch_initial()
        self.assertEqual(result["status"], "ineligible")
        self.assertEqual(self.implementer.requests, [])

    def test_confirmed_launch_persists_stable_execution_identity(self):
        stable_id = "conversation-012345"
        self.implementer.results.append(LaunchResult(LaunchDisposition.CONFIRMED, stable_id))
        result = self.dispatch_initial()
        self.assertEqual(result["execution_id"], stable_id)
        self.assertEqual(self.core.store.attempt(result["attempt_id"])["execution_id"], stable_id)

    def test_definitely_not_started_is_never_recorded_as_confirmed(self):
        self.implementer.results.append(LaunchResult(LaunchDisposition.DEFINITELY_NOT_STARTED))
        result = self.dispatch_initial()
        attempt = self.core.store.attempt(result["attempt_id"])
        self.assertEqual(result["status"], "launch_not_started")
        self.assertEqual(attempt["phase"], "LAUNCH_NOT_STARTED")
        self.assertIsNone(attempt["execution_id"])

    def test_unknown_launch_is_durable_and_never_relaunched(self):
        self.implementer.results.append(LaunchResult(LaunchDisposition.UNKNOWN))
        result = self.dispatch_initial()
        self.assertEqual(result["status"], "launch_unresolved")
        self.assertEqual(self.core.store.attempt(result["attempt_id"])["phase"], "LAUNCH_UNKNOWN")
        self.workflow.set_snapshot(snapshot(state="agent-ready", revision=30))
        restarted = RuntimeCore(self.db, MANIFEST_V2, self.workflow, self.implementer)
        again = restarted.dispatch_initial("acme/alpha", 7, "worker")
        self.assertEqual(again["status"], "initial_attempt_already_recorded")
        self.assertEqual(len(self.implementer.requests), 1)

    def test_expired_lease_alone_does_not_authorize_another_worker(self):
        self.establish_flow()
        with closing(sqlite3.connect(self.db)) as connection:
            connection.execute("UPDATE leases SET expires_at=0 WHERE repo='acme/alpha' AND issue_number=7")
            connection.commit()
        self.workflow.set_snapshot(snapshot(state="agent-ready", revision=30))
        result = self.core.dispatch_initial("acme/alpha", 7, "different-worker")
        self.assertEqual(result["status"], "lease_held")
        self.assertEqual(len(self.implementer.requests), 1)
        renewed = self.core.dispatch_initial("acme/alpha", 7, "worker")
        self.assertEqual(renewed["status"], "flow_already_started")
        with closing(sqlite3.connect(self.db)) as connection:
            expires_at = connection.execute(
                "SELECT expires_at FROM leases WHERE repo='acme/alpha' AND issue_number=7"
            ).fetchone()[0]
        self.assertGreater(expires_at, 0)

    def test_stale_repair_sha_produces_no_repair_launch(self):
        self.establish_flow()
        candidate = self.candidate(sha="b" * 40)
        self.prepare_repair(candidate)
        self.workflow.set_snapshot(replace(self.workflow.current, pr_head_sha="c" * 40))
        result = self.core.dispatch_repair(candidate)
        self.assertEqual(result["status"], "stale_or_ineligible_repair")
        self.assertEqual(len(self.implementer.requests), 1)
        self.assertEqual(self.core.store.repair_attempts("acme/alpha", 7), [])

    def test_reviewer_repair_requires_formal_request_changes_on_current_head(self):
        self.establish_flow()
        candidate = self.candidate(cause="reviewer_rejection", cause_id="review-10")
        self.workflow.set_snapshot(replace(
            repair_snapshot(candidate),
            formal_review_state="CHANGES_REQUESTED",
            formal_review_head_sha="b" * 40,
        ))
        result = self.core.dispatch_repair(candidate)
        self.assertEqual(result["status"], "stale_or_ineligible_repair")
        self.assertEqual(len(self.implementer.requests), 1)
        self.assertEqual(self.core.store.repair_attempts("acme/alpha", 7), [])

    def test_reviewer_repair_cause_id_must_match_fresh_formal_review(self):
        self.establish_flow()
        candidate = self.candidate(cause="reviewer_rejection", cause_id="review-10")
        self.workflow.set_snapshot(replace(repair_snapshot(candidate), formal_review_id="review-older"))
        result = self.core.dispatch_repair(candidate)
        self.assertEqual(result["status"], "stale_or_ineligible_repair")
        self.assertEqual(len(self.implementer.requests), 1)

    def test_duplicate_same_head_repair_has_one_logical_launch(self):
        self.establish_flow()
        candidate = self.candidate(cause="reviewer_rejection", cause_id="review-10")
        self.prepare_repair(candidate)
        first = self.core.dispatch_repair(candidate)
        self.workflow.set_snapshot(repair_snapshot(candidate, revision=40))
        duplicate = self.core.dispatch_repair(candidate)
        self.assertEqual(first["status"], "launched")
        self.assertEqual(duplicate["status"], "duplicate")
        self.assertEqual(len(self.implementer.requests), 2)
        self.assertEqual(self.core.store.repair_attempts("acme/alpha", 7)[0]["repair_key"],
                         compute_repair_key("acme/alpha", 44, REVISION))

    def test_mixed_ci_and_reviewer_repairs_share_ordinals_one_through_three(self):
        results = self.run_mixed_repairs()
        self.assertEqual([result["ordinal"] for result in results], [1, 2, 3])
        self.assertEqual([request.repair_cause_type for request in self.implementer.requests[1:]], [
            "implementation_failure", "reviewer_rejection", "implementation_failure"
        ])
        self.assertEqual([request.pr_number for request in self.implementer.requests[1:]], [44, 44, 44])
        self.assertEqual([request.branch for request in self.implementer.requests[1:]], [BRANCH] * 3)

    def test_fourth_repair_is_rejected_and_moves_issue_to_needs_human(self):
        self.run_mixed_repairs()
        candidate = self.candidate(sha="4" * 40, cause="reviewer_rejection", cause_id="review-4")
        self.workflow.set_snapshot(repair_snapshot(candidate, state="changes-requested", revision=50))
        result = self.core.dispatch_repair(candidate)
        self.assertEqual(result["status"], "budget_exhausted")
        self.assertTrue(result["needs_human"])
        self.assertTrue(result["needs_human_transitioned"])
        self.assertEqual(self.workflow.current.coordination_state, "needs-human")
        self.assertEqual(len(self.implementer.requests), 4)  # initial plus exactly three repairs
        self.assertEqual(self.core.store.repair_attempts("acme/alpha", 7)[-1]["phase"], "BUDGET_EXHAUSTED")

    def test_reviewer_repair_transitions_claim_and_preserves_same_pr_branch(self):
        self.establish_flow()
        candidate = self.candidate(cause="reviewer_rejection", cause_id="review-12")
        self.prepare_repair(candidate)
        result = self.core.dispatch_repair(candidate)
        self.assertEqual(result["status"], "launched")
        self.assertIn(("acme/alpha", 7, "changes-requested", "agent-working", "20"), self.workflow.transitions)
        request = self.implementer.requests[-1]
        self.assertEqual((request.pr_number, request.branch, request.expected_head_sha), (44, BRANCH, REVISION))

    def test_ci_repair_preserves_same_pr_branch_without_reclassifying_cause(self):
        self.establish_flow()
        candidate = self.candidate(cause="implementation_failure", cause_id="workflow-run-33")
        self.prepare_repair(candidate)
        result = self.core.dispatch_repair(candidate)
        self.assertEqual(result["status"], "launched")
        request = self.implementer.requests[-1]
        self.assertEqual(request.repair_cause_type, "implementation_failure")
        self.assertEqual((request.pr_number, request.branch), (44, BRANCH))
        self.assertFalse(any(edge[3] == "agent-working" for edge in self.workflow.transitions[1:]))

    def test_missing_local_flow_provenance_fails_closed(self):
        candidate = self.candidate(cause="reviewer_rejection")
        self.prepare_repair(candidate)
        result = self.core.dispatch_repair(candidate)
        self.assertEqual(result["status"], "untrusted_provenance")
        self.assertEqual(self.implementer.requests, [])

    def test_infrastructure_and_ambiguous_repair_causes_are_not_admitted(self):
        self.establish_flow()
        for cause in ("infrastructure_failure", "ambiguous_failure"):
            candidate = self.candidate(sha="b" * 40, cause=cause, cause_id=f"cause-{cause}")
            self.prepare_repair(candidate)
            result = self.core.dispatch_repair(candidate)
            self.assertEqual(result["status"], "unsupported_repair_cause")
        self.assertEqual(len(self.implementer.requests), 1)

    def test_unknown_repair_launch_invalidates_flow_trust_for_new_heads(self):
        self.establish_flow()
        first = self.candidate(cause="reviewer_rejection", cause_id="review-unknown")
        self.prepare_repair(first)
        self.implementer.results.append(LaunchResult(LaunchDisposition.UNKNOWN))
        result = self.core.dispatch_repair(first)
        self.assertEqual(result["status"], "launch_unresolved")
        next_candidate = self.candidate(sha="b" * 40, cause="implementation_failure", cause_id="next-check")
        self.workflow.set_snapshot(repair_snapshot(next_candidate, state="agent-working", revision=80))
        next_result = self.core.dispatch_repair(next_candidate)
        self.assertEqual(next_result["status"], "untrusted_provenance")
        self.assertEqual(len(self.implementer.requests), 2)

    def test_schema_v1_runtime_configuration_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "schema_version 2"):
            RuntimeCore(self.db, {"schema_version": 1}, self.workflow, self.implementer)

    def test_two_consumer_fixtures_share_core_without_ledger_collision(self):
        self.establish_flow("acme/alpha", 7, "worker")
        other_workflow = FakeWorkflow(snapshot(repo="contoso/beta", issue=7))
        other_core = RuntimeCore(self.db, MANIFEST_V2, other_workflow, self.implementer)
        initial = other_core.dispatch_initial("contoso/beta", 7, "worker")
        self.assertEqual(initial["status"], "launched")

        for repo, flow_core, flow_workflow, sha in [
            ("acme/alpha", self.core, self.workflow, "a" * 40),
            ("contoso/beta", other_core, other_workflow, "b" * 40),
        ]:
            candidate = self.candidate(sha=sha, cause="implementation_failure", cause_id=f"check-{repo}", repo=repo)
            flow_workflow.set_snapshot(repair_snapshot(candidate))
            self.assertEqual(flow_core.dispatch_repair(candidate)["ordinal"], 1)
        self.assertEqual(len(self.core.store.repair_attempts("acme/alpha", 7)), 1)
        self.assertEqual(len(self.core.store.repair_attempts("contoso/beta", 7)), 1)
        self.assertNotEqual(
            compute_repair_key("acme/alpha", 44, "a" * 40),
            compute_repair_key("contoso/beta", 44, "a" * 40),
        )

    def test_concurrent_same_head_repair_is_admitted_once(self):
        self.establish_flow()
        duplicate = self.candidate(sha="3" * 40, cause_id="check-3")
        self.workflow.set_snapshot(repair_snapshot(duplicate, state="agent-working", revision=31))
        barrier = threading.Barrier(2)
        self.workflow.observe_barrier = barrier
        other = RuntimeCore(self.db, MANIFEST_V2, self.workflow, self.implementer)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda core: core.dispatch_repair(duplicate), (self.core, other)))
        self.assertEqual(sum(result["status"] == "launched" for result in results), 1)
        self.assertEqual(sum(result["status"] == "duplicate" for result in results), 1)
        self.assertEqual(len(self.core.store.repair_attempts("acme/alpha", 7)), 1)

    def test_concurrent_third_and_fourth_boundary_never_admits_repair_four(self):
        self.establish_flow()
        for ordinal, cause in ((1, "implementation_failure"), (2, "reviewer_rejection")):
            candidate = self.candidate(sha=f"{ordinal:040x}", cause=cause, cause_id=f"cause-{ordinal}")
            state = "changes-requested" if cause == "reviewer_rejection" else "agent-working"
            self.workflow.set_snapshot(repair_snapshot(candidate, state=state, revision=20 + ordinal))
            self.assertEqual(self.core.dispatch_repair(candidate)["ordinal"], ordinal)

        third = self.candidate(sha="3" * 40, cause="implementation_failure", cause_id="check-3")
        fourth = self.candidate(sha="4" * 40, cause="reviewer_rejection", cause_id="review-4")
        snapshots = {
            "race_0": repair_snapshot(third, state="agent-working", revision=33),
            "race_1": repair_snapshot(fourth, state="changes-requested", revision=34),
        }
        routed = ThreadRoutedWorkflow(snapshots, threading.Barrier(2))
        third_core = RuntimeCore(self.db, MANIFEST_V2, routed, self.implementer)
        fourth_core = RuntimeCore(self.db, MANIFEST_V2, routed, self.implementer)
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="race") as pool:
            futures = [
                pool.submit(third_core.dispatch_repair, third),
                pool.submit(fourth_core.dispatch_repair, fourth),
            ]
            # Use deterministic names for the per-worker fresh facts.
            results = [future.result() for future in futures]
        self.assertEqual({result["status"] for result in results}, {"launched", "budget_exhausted"})
        self.assertEqual(len(self.implementer.requests), 4)  # initial plus repair ordinals 1..3
        attempts = self.core.store.repair_attempts("acme/alpha", 7)
        self.assertEqual([attempt["repair_ordinal"] for attempt in attempts], [1, 2, 3, 4])
        self.assertEqual(attempts[-1]["phase"], "BUDGET_EXHAUSTED")


if __name__ == "__main__":
    unittest.main()
