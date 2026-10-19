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
from vccp_runtime.core import _ci_cause_is_current


REVISION = "a" * 40
BRANCH = "implement/issue-7"
PROVIDER = "antigravity"
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
        canonical_relationship_valid=True,
        canonical_link_count=1,
        open_linked_pr_count=1,
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
                    terminal_labels=(new_state,) if new_state in {"needs-human", "infra-blocked"} else (),
                    revision=f"{int(self.current.revision) + 1}",
                )
            return True

    def restore_repair_coordination_state(self, repo, issue_number, new_state, expected_revision):
        return self.transition_coordination_state(repo, issue_number, "needs-human", new_state, expected_revision)


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
        return LaunchResult(LaunchDisposition.CONFIRMED, f"execution-{len(self.requests)}", PROVIDER)

    def latest_activity_for(self, provider, execution_id):
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
        before_deadline = self.core.observe_implementer_progress("acme/alpha", baseline + 20 + 1799)
        self.assertEqual(before_deadline["items"][0]["status"], "no_new_activity")
        self.assertEqual(self.workflow.current.coordination_state, "agent-working")
        outcome = self.core.observe_implementer_progress("acme/alpha", baseline + 20 + 1800)
        self.assertEqual(outcome["items"][0]["status"], "implementer_stalled")
        self.assertEqual(self.workflow.current.coordination_state, "infra-blocked")

    def test_launch_recovery_and_progress_deadlines_use_separate_timeouts(self):
        self.core = RuntimeCore(self.db, MANIFEST_V2, self.workflow, self.implementer,
                                recovery_timeout_seconds=900,
                                implementer_progress_timeout_seconds=1800)
        confirmed = self.dispatch_initial()
        attempt = self.core.store.attempt(confirmed["attempt_id"])
        baseline = attempt["implementer_activity_at"]
        self.assertEqual(attempt["progress_deadline_at"] - baseline, 1800)
        self.assertIsNone(attempt["deadline_at"])

    def test_unknown_launch_uses_recovery_timeout_independent_of_progress_timeout(self):
        for progress_timeout in (1800, 37):
            with self.subTest(progress_timeout=progress_timeout), tempfile.TemporaryDirectory() as folder:
                workflow = FakeWorkflow(snapshot())
                implementer = FakeImplementer([LaunchResult(LaunchDisposition.UNKNOWN, provider=PROVIDER)])
                core = RuntimeCore(Path(folder) / "runtime.db", MANIFEST_V2, workflow, implementer,
                                   recovery_timeout_seconds=900,
                                   implementer_progress_timeout_seconds=progress_timeout)
                result = core.dispatch_initial("acme/alpha", 7, "worker")
                self.assertEqual(result["phase"], "LAUNCH_UNKNOWN")
                attempt = core.store.attempt(result["attempt_id"])
                self.assertEqual(attempt["deadline_at"] - attempt["phase_entered_at"], 900)
                self.assertIsNone(attempt["implementer_activity_at"])
                self.assertIsNone(attempt["progress_deadline_at"])

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
        self.implementer.results.append(LaunchResult(LaunchDisposition.CONFIRMED, stable_id, PROVIDER))
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
        self.implementer.results.append(LaunchResult(LaunchDisposition.UNKNOWN, provider=PROVIDER))
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

    def test_mixed_ci_and_reviewer_repairs_share_global_ordinals_one_through_three(self):
        results = self.run_mixed_repairs()
        self.assertEqual([result["ordinal"] for result in results], [1, 2, 3])
        self.assertEqual([request.repair_cause_type for request in self.implementer.requests[1:]], [
            "implementation_failure", "reviewer_rejection", "implementation_failure"
        ])
        self.assertEqual([request.pr_number for request in self.implementer.requests[1:]], [44, 44, 44])
        self.assertEqual([request.branch for request in self.implementer.requests[1:]], [BRANCH] * 3)

    def test_global_budget_exhaustion_moves_issue_to_needs_human(self):
        self.establish_flow()
        sequence = [("implementation_failure", f"ci-{n}") for n in range(1, 4)] + [
            ("reviewer_rejection", "review-4"), ("reviewer_rejection", "review-5")]
        for ordinal, (cause, cause_id) in enumerate(sequence, 1):
            candidate = self.candidate(sha=f"{ordinal:040x}", cause=cause, cause_id=cause_id)
            state = "changes-requested" if cause == "reviewer_rejection" else "agent-working"
            self.workflow.set_snapshot(repair_snapshot(candidate, state=state, revision=40 + ordinal))
            self.assertEqual(self.core.dispatch_repair(candidate)["status"], "launched")
        candidate = self.candidate(sha="6" * 40, cause="reviewer_rejection", cause_id="review-6")
        self.workflow.set_snapshot(repair_snapshot(candidate, state="changes-requested", revision=50))
        result = self.core.dispatch_repair(candidate)
        self.assertEqual(result["status"], "budget_exhausted")
        self.assertTrue(result["needs_human"])
        self.assertTrue(result["needs_human_transitioned"])
        self.assertEqual(self.workflow.current.coordination_state, "needs-human")
        self.assertEqual(len(self.implementer.requests), 6)  # initial plus the global cap of five
        self.assertEqual(self.core.store.repair_attempts("acme/alpha", 7)[-1]["phase"], "BUDGET_EXHAUSTED")

    def test_classified_budgets_allow_review_after_ci_repairs_and_human_grant_resumes(self):
        self.establish_flow()
        sequence = [("implementation_failure", f"ci-{n}") for n in range(1, 4)] + [
            ("reviewer_rejection", "review-4"), ("reviewer_rejection", "review-5")]
        for ordinal, (cause, cause_id) in enumerate(sequence, 1):
            candidate = self.candidate(sha=f"{ordinal:040x}", cause=cause, cause_id=cause_id)
            state = "changes-requested" if cause == "reviewer_rejection" else "agent-working"
            self.workflow.set_snapshot(repair_snapshot(candidate, state=state, revision=30 + ordinal))
            self.assertEqual(self.core.dispatch_repair(candidate)["status"], "launched")
        # CI cap is reached, but the separate Reviewer allowance remains usable.
        sixth = self.candidate(sha="6" * 40, cause="reviewer_rejection", cause_id="review-6")
        self.workflow.set_snapshot(repair_snapshot(sixth, state="changes-requested", revision=50))
        self.assertEqual(self.core.dispatch_repair(sixth)["status"], "budget_exhausted")
        # Simulate legacy BUDGET_EXHAUSTED terminal state from the current runtime.
        self.workflow.set_snapshot(replace(self.workflow.current, coordination_state="needs-human",
                                           terminal_labels=("needs-human",), revision="51"))
        resumed = self.core.authorize_repair_continuation(sixth, "maintainer@example.test")
        self.assertEqual(resumed["status"], "authorized")
        self.assertEqual(self.workflow.current.coordination_state, "changes-requested")
        replay = self.core.authorize_repair_continuation(sixth, "maintainer@example.test")
        self.assertEqual(replay["status"], "already_authorized")
        restarted = RuntimeCore(self.db, MANIFEST_V2, self.workflow, self.implementer)
        self.assertEqual(restarted.authorize_repair_continuation(sixth, "maintainer@example.test")["status"],
                         "already_authorized")
        dispatched = restarted.dispatch_repair(sixth)
        self.assertEqual(dispatched["status"], "launched")
        self.assertEqual(dispatched["ordinal"], 6)
        self.assertIsNotNone(restarted.store.continuation_for_candidate(sixth)["consumed_at"])
        self.assertEqual(restarted.authorize_repair_continuation(sixth, "maintainer")["status"],
                         "already_authorized")
        launches = len(self.implementer.requests)
        self.assertEqual(restarted.dispatch_repair(sixth)["status"], "stale_or_ineligible_repair")
        self.assertEqual(len(self.implementer.requests), launches)
        self.assertEqual(len(self.implementer.requests), 7)
        next_head = self.candidate(sha="7" * 40, cause="reviewer_rejection", cause_id="review-7")
        self.workflow.set_snapshot(repair_snapshot(next_head, state="changes-requested", revision=60))
        self.assertEqual(self.core.dispatch_repair(next_head)["status"], "budget_exhausted")

    def test_issue57_three_mixed_repairs_do_not_block_next_reviewer_repair(self):
        self.establish_flow()
        sequence = [("implementation_failure", "browser-e2e"),
                    ("reviewer_rejection", "review-5-findings"),
                    ("implementation_failure", "whitespace-guard")]
        for ordinal, (cause, cause_id) in enumerate(sequence, 1):
            candidate = self.candidate(sha=f"{ordinal:040x}", cause=cause, cause_id=cause_id)
            state = "changes-requested" if cause == "reviewer_rejection" else "agent-working"
            self.workflow.set_snapshot(repair_snapshot(candidate, state=state, revision=20 + ordinal))
            self.assertEqual(self.core.dispatch_repair(candidate)["status"], "launched")
        fourth = self.candidate(sha="4" * 40, cause="reviewer_rejection", cause_id="review-ac6")
        self.workflow.set_snapshot(repair_snapshot(fourth, state="changes-requested", revision=40))
        self.assertEqual(self.core.store.flow("acme/alpha", 7)["repair_count"], 3)
        result = self.core.dispatch_repair(fourth)
        self.assertEqual(result["status"], "launched")
        self.assertEqual(result["ordinal"], 4)

    def test_legacy_budget_exhausted_at_three_repairs_resumes_same_issue_and_pr(self):
        self.establish_flow()
        sequence = [("implementation_failure", "browser-e2e"),
                    ("reviewer_rejection", "review-five-findings"),
                    ("implementation_failure", "whitespace-guard")]
        for ordinal, (cause, cause_id) in enumerate(sequence, 1):
            prior = self.candidate(sha=f"{ordinal:040x}", cause=cause, cause_id=cause_id)
            state = "changes-requested" if cause == "reviewer_rejection" else "agent-working"
            self.workflow.set_snapshot(repair_snapshot(prior, state=state, revision=20 + ordinal))
            self.assertEqual(self.core.dispatch_repair(prior)["status"], "launched")
        fourth = self.candidate(sha="4" * 40, cause="reviewer_rejection", cause_id="review-ac6")
        now = 1234567890.0
        with closing(sqlite3.connect(self.db)) as connection:
            connection.execute(
                "INSERT INTO attempts(attempt_id,repo,issue_number,owner_id,lease_token,kind,cause_type,cause_id,"
                "repair_key,repair_ordinal,expected_head_sha,pr_number,branch,phase,created_at,updated_at) "
                "VALUES(?,?,?,?,?,'repair',?,?,?,?,?,?,?,'BUDGET_EXHAUSTED',?,?)",
                ("legacy-exhaustion", "acme/alpha", 7, "worker", "", "reviewer_rejection", "review-ac6",
                 compute_repair_key("acme/alpha", 44, "4" * 40), 4, "4" * 40, 44, BRANCH, now, now),
            )
            connection.commit()
        # Opening an existing Consumer database migrates the old marker key in place.
        self.core = RuntimeCore(self.db, MANIFEST_V2, self.workflow, self.implementer)
        preserved = [row for row in self.core.store.repair_attempts("acme/alpha", 7)
                    if row["attempt_id"] == "legacy-exhaustion"]
        self.assertEqual(len(preserved), 1)
        self.assertEqual(preserved[0]["phase"], "BUDGET_EXHAUSTED")
        self.assertIn(":budget:legacy-exhaustion", preserved[0]["repair_key"])
        stopped = replace(repair_snapshot(fourth, state="needs-human", revision=50),
                          coordination_state="needs-human", terminal_labels=("needs-human",))
        self.workflow.set_snapshot(stopped)
        with ThreadPoolExecutor(max_workers=2) as pool:
            grants = list(pool.map(lambda _: self.core.store.authorize_continuation(fourth, "maintainer"), range(2)))
        self.assertEqual({grant["status"] for grant in grants},
                         {"authorized_pending_activation", "already_authorized"})
        authorization = self.core.authorize_repair_continuation(fourth, "maintainer")
        self.assertIn(authorization["status"], {"authorized", "already_authorized"})
        with closing(sqlite3.connect(self.db)) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM repair_continuations").fetchone()[0], 1)
        restarted = RuntimeCore(self.db, MANIFEST_V2, self.workflow, self.implementer)
        self.assertEqual(restarted.authorize_repair_continuation(fourth, "maintainer")["status"],
                         "already_authorized")
        repair = restarted.dispatch_repair(fourth)
        self.assertEqual((repair["status"], repair["ordinal"]), ("launched", 4))
        self.assertEqual(self.workflow.current.pr_number, 44)

    def test_explicit_grant_extends_only_the_exhausted_category_by_one(self):
        self.establish_flow()
        for ordinal in range(1, 4):
            prior = self.candidate(sha=f"{ordinal:040x}", cause="implementation_failure",
                                   cause_id=f"ci-{ordinal}")
            self.workflow.set_snapshot(repair_snapshot(prior, state="agent-working", revision=20 + ordinal))
            self.assertEqual(self.core.dispatch_repair(prior)["status"], "launched")
        head = "4" * 40
        candidate = self.candidate(sha=head, cause="implementation_failure",
                                   cause_id=f"ci:{head}:required-check:44:failure")
        check = {"name": "required-check", "id": 44, "status": "completed", "conclusion": "failure",
                 "head_sha": head}
        self.workflow.set_snapshot(replace(repair_snapshot(candidate, state="agent-working", revision=30),
                                           check_runs=(check,)))
        self.assertEqual(self.core.dispatch_repair(candidate)["status"], "budget_exhausted")
        self.workflow.set_snapshot(replace(self.workflow.current, coordination_state="needs-human",
                                           terminal_labels=("needs-human",), revision="31"))
        self.assertEqual(self.core.authorize_repair_continuation(candidate, "maintainer")["status"], "authorized")
        resumed = self.core.dispatch_repair(candidate)
        self.assertEqual((resumed["status"], resumed["ordinal"]), ("launched", 4))

    def test_three_continuations_cumulate_and_fourth_cannot_admit_attempt(self):
        self.establish_flow()
        # Five automatic repairs establish the global stop, including CI noise.
        for ordinal in range(1, 6):
            cause = "implementation_failure" if ordinal in {1, 3, 5} else "reviewer_rejection"
            candidate = self.candidate(sha=f"{ordinal:040x}", cause=cause,
                                       cause_id=f"review-{ordinal}" if cause == "reviewer_rejection"
                                       else f"ci-{ordinal}")
            state = "changes-requested" if cause == "reviewer_rejection" else "agent-working"
            self.workflow.set_snapshot(repair_snapshot(candidate, state=state, revision=20 + ordinal))
            self.assertEqual(self.core.dispatch_repair(candidate)["status"], "launched")

        for grant_number in range(1, 4):
            head = f"{5 + grant_number:040x}"
            candidate = self.candidate(sha=head, cause="reviewer_rejection",
                                       cause_id=f"review-authorized-{grant_number}")
            current = repair_snapshot(candidate, state="changes-requested", revision=30 + grant_number)
            self.workflow.set_snapshot(current)
            self.assertEqual(self.core.dispatch_repair(candidate)["status"], "budget_exhausted")
            self.assertEqual(self.core.authorize_repair_continuation(
                candidate, "maintainer")["status"], "authorized")
            self.workflow.set_snapshot(repair_snapshot(candidate, state="changes-requested",
                                                        revision=50 + grant_number))
            result = self.core.dispatch_repair(candidate)
            self.assertEqual((result["status"], result["ordinal"]), ("launched", 5 + grant_number))
            if grant_number < 3:
                self.assertEqual(self.core.store.flow("acme/alpha", 7)["repair_count"], 5 + grant_number)

        self.assertEqual(len([row for row in self.core.store.repair_attempts("acme/alpha", 7)
                              if row["phase"] != "BUDGET_EXHAUSTED"]), 8)
        denied = self.candidate(sha="9" * 40, cause="reviewer_rejection", cause_id="review-ninth")
        self.workflow.set_snapshot(repair_snapshot(denied, state="changes-requested", revision=70))
        self.assertEqual(self.core.dispatch_repair(denied)["status"], "budget_exhausted")
        self.assertEqual(self.core.authorize_repair_continuation(denied, "maintainer")["status"],
                         "continuation_limit_exhausted")
        self.assertEqual(len(self.implementer.requests), 9)  # initial plus eight repairs

    def test_grant_for_one_head_and_cause_does_not_expand_automatic_budget(self):
        self.establish_flow()
        for ordinal in range(1, 6):
            cause = "implementation_failure" if ordinal in {1, 3, 5} else "reviewer_rejection"
            candidate = self.candidate(sha=f"{ordinal:040x}", cause=cause,
                                       cause_id=f"cause-{ordinal}")
            state = "agent-working" if cause == "implementation_failure" else "changes-requested"
            self.workflow.set_snapshot(repair_snapshot(candidate, state=state, revision=20 + ordinal))
            self.assertEqual(self.core.dispatch_repair(candidate)["status"], "launched")

        authorized = self.candidate(sha="6" * 40, cause="reviewer_rejection", cause_id="review-A")
        self.workflow.set_snapshot(repair_snapshot(authorized, state="changes-requested", revision=30))
        self.assertEqual(self.core.dispatch_repair(authorized)["status"], "budget_exhausted")
        self.workflow.set_snapshot(replace(self.workflow.current, coordination_state="needs-human",
                                           terminal_labels=("needs-human",), revision="31"))
        self.assertEqual(self.core.authorize_repair_continuation(authorized, "maintainer")["status"],
                         "authorized")

        other_cause = self.candidate(sha="7" * 40, cause="reviewer_rejection", cause_id="review-B")
        self.workflow.set_snapshot(repair_snapshot(other_cause, state="changes-requested", revision=32))
        self.assertEqual(self.core.dispatch_repair(other_cause)["status"], "budget_exhausted")
        self.assertEqual(len(self.implementer.requests), 6)  # initial plus five automatic repairs

    def test_ci_grant_cannot_be_spent_by_reviewer_candidate(self):
        self.establish_flow()
        for ordinal in range(1, 6):
            cause = "implementation_failure" if ordinal in {1, 3, 5} else "reviewer_rejection"
            candidate = self.candidate(sha=f"{ordinal:040x}", cause=cause,
                                       cause_id=f"cause-{ordinal}")
            state = "agent-working" if cause == "implementation_failure" else "changes-requested"
            self.workflow.set_snapshot(repair_snapshot(candidate, state=state, revision=20 + ordinal))
            self.assertEqual(self.core.dispatch_repair(candidate)["status"], "launched")

        ci_head = "6" * 40
        ci = self.candidate(sha=ci_head, cause="implementation_failure",
                            cause_id=f"ci:{ci_head}:Unit:66:failure")
        self.workflow.set_snapshot(replace(repair_snapshot(ci, state="agent-working", revision=30),
                                           check_runs=({"name": "Unit", "id": 66, "head_sha": ci_head,
                                                        "status": "completed", "conclusion": "failure"},)))
        self.assertEqual(self.core.dispatch_repair(ci)["status"], "budget_exhausted")
        self.workflow.set_snapshot(replace(self.workflow.current, coordination_state="needs-human",
                                           terminal_labels=("needs-human",), revision="31"))
        self.assertEqual(self.core.authorize_repair_continuation(ci, "maintainer")["status"], "authorized")

        reviewer = self.candidate(sha="7" * 40, cause="reviewer_rejection", cause_id="review-B")
        self.workflow.set_snapshot(repair_snapshot(reviewer, state="changes-requested", revision=32))
        self.assertEqual(self.core.dispatch_repair(reviewer)["status"], "budget_exhausted")
        self.assertEqual(len(self.implementer.requests), 6)

    def test_ci_cause_requires_latest_run_for_same_required_check_and_head(self):
        candidate = self.candidate(sha="d" * 40, cause="implementation_failure",
                                   cause_id=f"ci:{'d' * 40}:Unit:100:failure")
        old_failure = {"name": "Unit", "id": 100, "status": "completed", "conclusion": "failure",
                       "head_sha": candidate.head_sha, "started_at": "2026-10-19T10:00:00Z"}
        new_success = {"name": "Unit", "id": 101, "status": "completed", "conclusion": "success",
                       "head_sha": candidate.head_sha, "started_at": "2026-10-19T10:01:00Z"}
        observed = replace(repair_snapshot(candidate, state="needs-human"),
                           check_runs=(old_failure, new_success))
        self.assertFalse(_ci_cause_is_current(candidate, observed))
        latest_failure = {**new_success, "conclusion": "failure"}
        current_candidate = replace(candidate, cause_id=f"ci:{'d' * 40}:Unit:101:failure")
        self.assertTrue(_ci_cause_is_current(current_candidate,
                                             replace(observed, check_runs=(old_failure, latest_failure))))
        self.assertFalse(_ci_cause_is_current(current_candidate,
                                              replace(observed, check_runs=(old_failure,
                                                  {**new_success, "id": 101, "status": "queued"}))))

    def test_continuation_rejects_changed_head_or_stale_review(self):
        self.establish_flow()
        candidate = self.candidate(sha="1" * 40, cause="reviewer_rejection", cause_id="review-stale")
        self.workflow.set_snapshot(repair_snapshot(candidate, state="changes-requested", revision=30))
        # Fill to the global boundary while preserving a real exhaustion row for this cause.
        sequence = [("implementation_failure", f"ci-{n}") for n in range(1, 4)] + [
            ("reviewer_rejection", "review-4"), ("reviewer_rejection", "review-5")]
        for ordinal, (cause, cause_id) in enumerate(sequence, 1):
            other = self.candidate(sha=f"{ordinal + 1:040x}", cause=cause, cause_id=cause_id)
            state = "changes-requested" if cause == "reviewer_rejection" else "agent-working"
            self.workflow.set_snapshot(repair_snapshot(other, state=state, revision=30 + ordinal))
            self.core.dispatch_repair(other)
        # Force a budget-exhausted attempt bound to the requested reviewer evidence.
        self.workflow.set_snapshot(repair_snapshot(candidate, state="changes-requested", revision=50))
        self.assertEqual(self.core.dispatch_repair(candidate)["status"], "budget_exhausted")
        self.workflow.set_snapshot(replace(self.workflow.current, coordination_state="needs-human",
                                           terminal_labels=("needs-human",), revision="51",
                                           pr_head_sha="f" * 40))
        self.assertEqual(self.core.authorize_repair_continuation(candidate, "maintainer")["status"],
                         "authorization_evidence_stale_or_invalid")
        same_head = repair_snapshot(candidate, state="needs-human", revision=52)
        self.workflow.set_snapshot(replace(same_head, coordination_state="needs-human",
                                           terminal_labels=("needs-human",), pr_open=False))
        self.assertEqual(self.core.authorize_repair_continuation(candidate, "maintainer")["status"],
                         "authorization_evidence_stale_or_invalid")
        updated_review = repair_snapshot(candidate, state="needs-human", revision=53)
        self.workflow.set_snapshot(replace(updated_review, coordination_state="needs-human",
                                           terminal_labels=("needs-human",), formal_review_state="APPROVED"))
        self.assertEqual(self.core.authorize_repair_continuation(candidate, "maintainer")["status"],
                         "authorization_evidence_stale_or_invalid")

    def test_continuation_rejects_changed_ci_evidence(self):
        self.establish_flow()
        sequence = [("implementation_failure", f"ci-{n}") for n in range(1, 4)] + [
            ("reviewer_rejection", "review-4"), ("reviewer_rejection", "review-5")]
        for ordinal, (cause, cause_id) in enumerate(sequence, 1):
            prior = self.candidate(sha=f"{ordinal:040x}", cause=cause, cause_id=cause_id)
            state = "changes-requested" if cause == "reviewer_rejection" else "agent-working"
            self.workflow.set_snapshot(repair_snapshot(prior, state=state, revision=30 + ordinal))
            self.core.dispatch_repair(prior)
        head = "6" * 40
        cause_id = f"ci:{head}:required-check:99:failure"
        candidate = self.candidate(sha=head, cause="implementation_failure", cause_id=cause_id)
        check = {"name": "required-check", "id": 99, "status": "completed", "conclusion": "failure",
                 "head_sha": head}
        observed = replace(repair_snapshot(candidate, state="agent-working", revision=50), check_runs=(check,))
        self.workflow.set_snapshot(observed)
        self.assertEqual(self.core.dispatch_repair(candidate)["status"], "budget_exhausted")
        self.workflow.set_snapshot(replace(self.workflow.current, coordination_state="needs-human",
                                           terminal_labels=("needs-human",), revision="51", check_runs=()))
        self.assertEqual(self.core.authorize_repair_continuation(candidate, "maintainer")["status"],
                         "authorization_evidence_stale_or_invalid")

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
        self.implementer.results.append(LaunchResult(LaunchDisposition.UNKNOWN, provider=PROVIDER))
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

    def test_concurrent_repairs_respect_updated_global_boundary(self):
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
        self.assertEqual({result["status"] for result in results}, {"launched"})
        self.assertEqual(len(self.implementer.requests), 5)  # initial plus repair ordinals 1..4
        attempts = self.core.store.repair_attempts("acme/alpha", 7)
        self.assertEqual([attempt["repair_ordinal"] for attempt in attempts], [1, 2, 3, 4])
        self.assertTrue(all(attempt["phase"] != "BUDGET_EXHAUSTED" for attempt in attempts))


if __name__ == "__main__":
    unittest.main()
