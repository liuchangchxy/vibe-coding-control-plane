import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from dataclasses import replace
from pathlib import Path

from vccp_runtime.core import (LaunchDisposition, LaunchResult, RepairCandidate, RuntimeCore,
                               WorkflowSnapshot)


MANIFEST = {"schema_version": 2, "issue_contract": {"max_automated_repairs": 3}}
SHA = "a" * 40
BRANCH = "implement/7"


def snap(repo="acme/alpha", issue=7, state="agent-ready", revision="1", **kwargs):
    return WorkflowSnapshot(repo=repo, issue_number=issue, revision=revision, issue_open=True,
                            coordination_state=state, frozen_spec=True, **kwargs)


class RecoveryWorkflow:
    def __init__(self, *snapshots):
        self.states = {(s.repo.casefold(), s.issue_number): s for s in snapshots}
        self.transitions = []
        self.observe_count = 0
        self.observe_hook = None

    def observe(self, repo, issue_number):
        self.observe_count += 1
        if self.observe_hook:
            self.observe_hook(self, repo, issue_number, self.observe_count)
        return self.states[(repo.casefold(), issue_number)]

    def discover_active(self, repo):
        return sorted(issue for (identity, issue), state in self.states.items()
                      if identity == repo.casefold() and state.issue_open
                      and state.coordination_state in {"agent-working", "changes-requested"})

    def transition_coordination_state(self, repo, issue_number, expected_state, new_state, expected_revision):
        key = (repo.casefold(), issue_number)
        current = self.states[key]
        self.transitions.append((repo, issue_number, expected_state, new_state, expected_revision))
        if current.coordination_state != expected_state or current.revision != expected_revision:
            return False
        terminal = (new_state,) if new_state in {"infra-blocked", "needs-human"} else ()
        self.states[key] = replace(current, coordination_state=new_state,
                                   revision=f"{current.revision}+", terminal_labels=terminal)
        return True


class RecoveryImplementer:
    def __init__(self, *results):
        self.results = list(results)
        self.requests = []
        self.activity = None

    def launch(self, request):
        self.requests.append(request)
        return self.results.pop(0) if self.results else LaunchResult(LaunchDisposition.CONFIRMED, "exec")

    def latest_activity(self, conversation_id):
        return self.activity


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "runtime.sqlite3"
        self.workflow = RecoveryWorkflow(snap())
        self.implementer = RecoveryImplementer()
        self.core = RuntimeCore(self.db, MANIFEST, self.workflow, self.implementer,
                                lease_ttl_seconds=30, recovery_timeout_seconds=60)

    def tearDown(self):
        self.temp.cleanup()

    def set_snapshot(self, value):
        self.workflow.states[(value.repo.casefold(), value.issue_number)] = value

    def add_pr(self, *, head=SHA, number=44, branch=BRANCH, issue=7, repo="acme/alpha"):
        self.set_snapshot(replace(self.workflow.observe(repo, issue), pr_number=number, pr_open=True,
                                  pr_linked_issue=issue, pr_head_sha=head, pr_branch=branch,
                                  open_linked_pr_count=1, canonical_link_count=1,
                                  canonical_relationship_valid=True))

    def start(self, disposition=LaunchDisposition.CONFIRMED):
        self.implementer.results = [LaunchResult(disposition, "execution" if disposition == LaunchDisposition.CONFIRMED else None)]
        return self.core.dispatch_initial("acme/alpha", 7, "owner")

    def test_restart_after_launch_confirmed_binds_canonical_pr_without_duplicate_launch(self):
        self.assertEqual(self.start()["phase"], "LAUNCH_CONFIRMED")
        self.add_pr()
        restarted = RuntimeCore(self.db, MANIFEST, self.workflow, self.implementer,
                                recovery_timeout_seconds=60)
        result = restarted.reconcile_once("acme/alpha", "owner")
        self.assertEqual(result["items"][0]["status"], "pr_bound")
        self.assertEqual(restarted.store.attempts_for_repo("acme/alpha")[-1]["phase"], "PR_BOUND")
        self.assertEqual(len(self.implementer.requests), 1)

    def _start_repair(self):
        self.start()
        self.add_pr()
        self.set_snapshot(replace(self.workflow.observe("acme/alpha", 7), pr_head_sha=SHA))
        candidate = RepairCandidate("acme/alpha", 7, 44, SHA, BRANCH,
                                    "implementation_failure", "check-1", "owner")
        result = self.core.dispatch_repair(candidate)
        self.assertEqual(result["phase"], "LAUNCH_CONFIRMED")
        return result

    def test_restart_repair_with_unchanged_head_waits_without_relaunch(self):
        self._start_repair()
        row = self.core.store.attempts_for_repo("acme/alpha")[-1]
        core = RuntimeCore(self.db, MANIFEST, self.workflow, self.implementer, recovery_timeout_seconds=60)
        result = core.reconcile_once("acme/alpha", "owner", row["implementer_activity_at"] + 61)
        self.assertEqual(result["items"][0]["status"], "waiting_for_repair_push")
        self.assertEqual(self.workflow.observe("acme/alpha", 7).coordination_state, "agent-working")
        self.assertEqual(len(self.implementer.requests), 2)

    def test_restart_repair_with_new_head_binds_same_pr_and_branch(self):
        self._start_repair()
        new_head = "b" * 40
        self.add_pr(head=new_head)
        core = RuntimeCore(self.db, MANIFEST, self.workflow, self.implementer, recovery_timeout_seconds=60)
        result = core.reconcile_once("acme/alpha", "owner")
        self.assertEqual(result["items"][0]["status"], "pr_bound")
        row = core.store.attempts_for_repo("acme/alpha")[-1]
        self.assertEqual((row["pr_number"], row["branch"], row["expected_head_sha"],
                          row["resulting_head_sha"], row["phase"]),
                         (44, BRANCH, SHA, new_head, "PR_BOUND"))
        self.assertEqual(len(self.implementer.requests), 2)

    def test_existing_database_migrates_without_losing_repair_baseline(self):
        self._start_repair()
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute("ALTER TABLE attempts DROP COLUMN resulting_head_sha")
            conn.commit()
        migrated = RuntimeCore(self.db, MANIFEST, self.workflow, self.implementer, recovery_timeout_seconds=60)
        row = migrated.store.attempts_for_repo("acme/alpha")[-1]
        self.assertEqual(row["expected_head_sha"], SHA)
        self.assertIsNone(row["resulting_head_sha"])
        self.add_pr(head="b" * 40)
        migrated.reconcile_once("acme/alpha", "owner")
        row = migrated.store.attempts_for_repo("acme/alpha")[-1]
        self.assertEqual((row["expected_head_sha"], row["resulting_head_sha"]), (SHA, "b" * 40))

    def test_unknown_with_canonical_pr_binds_without_relaunch(self):
        self.assertEqual(self.start(LaunchDisposition.UNKNOWN)["phase"], "LAUNCH_UNKNOWN")
        self.add_pr()
        restarted = RuntimeCore(self.db, MANIFEST, self.workflow, self.implementer)
        self.assertEqual(restarted.reconcile_once("acme/alpha", "owner")["items"][0]["status"], "pr_bound")
        self.assertEqual(len(self.implementer.requests), 1)

    def test_unknown_agent_ready_is_never_retry_eligible(self):
        self.start(LaunchDisposition.UNKNOWN)
        self.set_snapshot(replace(self.workflow.observe("acme/alpha", 7),
                                  coordination_state="agent-ready", revision="ready-after-unknown"))
        result = self.core.reconcile_once("acme/alpha", "owner")
        row = self.core.store.attempts_for_repo("acme/alpha")[-1]
        self.assertEqual(result["items"][0]["status"], "unresolved_claim_contradiction")
        self.assertEqual(row["phase"], "LAUNCH_UNKNOWN")
        self.assertEqual(self.core.dispatch_initial("acme/alpha", 7, "owner")["status"],
                         "initial_attempt_already_recorded")
        self.assertEqual(len(self.implementer.requests), 1)

    def test_confirmed_agent_ready_is_never_retry_eligible(self):
        self.start(LaunchDisposition.CONFIRMED)
        self.set_snapshot(replace(self.workflow.observe("acme/alpha", 7),
                                  coordination_state="agent-ready", revision="ready-after-confirmed"))
        result = self.core.reconcile_once("acme/alpha", "owner")
        row = self.core.store.attempts_for_repo("acme/alpha")[-1]
        self.assertEqual(result["items"][0]["status"], "unresolved_claim_contradiction")
        self.assertEqual(row["phase"], "LAUNCH_CONFIRMED")
        self.assertEqual(self.core.dispatch_initial("acme/alpha", 7, "owner")["status"], "flow_already_started")
        self.assertEqual(len(self.implementer.requests), 1)

    def test_unknown_without_evidence_before_deadline_stays_unresolved(self):
        self.start(LaunchDisposition.UNKNOWN)
        now = time.time()
        result = self.core.reconcile_once("acme/alpha", "owner", now)
        self.assertEqual(result["items"][0]["status"], "unresolved")
        self.assertEqual(len(self.implementer.requests), 1)

    def test_confirmed_launch_without_pr_waits_until_deadline(self):
        self.start(LaunchDisposition.CONFIRMED)
        row = self.core.store.attempts_for_repo("acme/alpha")[-1]
        baseline = row["implementer_activity_at"]
        self.implementer.activity = baseline
        result = self.core.reconcile_once("acme/alpha", "owner", baseline + 901)
        self.assertEqual(result["items"][0]["status"], "unresolved")
        self.assertEqual(len(self.implementer.requests), 1)
        before_stall = self.core.observe_implementer_progress("acme/alpha", baseline + 1799)
        self.assertEqual(before_stall["items"][0]["status"], "no_new_activity")
        self.assertEqual(self.workflow.observe("acme/alpha", 7).coordination_state, "agent-working")
        stalled = self.core.observe_implementer_progress("acme/alpha", baseline + 1800)
        self.assertEqual(stalled["items"][0]["status"], "implementer_stalled")
        self.assertEqual(self.workflow.observe("acme/alpha", 7).coordination_state, "infra-blocked")

    def test_confirmed_activity_after_recovery_boundary_refreshes_progress_deadline(self):
        self.core = RuntimeCore(self.db, MANIFEST, self.workflow, self.implementer,
                                recovery_timeout_seconds=900,
                                implementer_progress_timeout_seconds=1800)
        self.start(LaunchDisposition.CONFIRMED)
        row = self.core.store.attempts_for_repo("acme/alpha")[-1]
        baseline = row["implementer_activity_at"]
        self.assertIsNone(row["deadline_at"])
        self.implementer.activity = baseline + 901
        reconciled = self.core.reconcile_once("acme/alpha", "owner", baseline + 901)
        self.assertEqual(reconciled["items"][0]["status"], "unresolved")
        observed = self.core.observe_implementer_progress("acme/alpha", baseline + 901)
        self.assertEqual(observed["items"][0]["status"], "progress")
        updated = self.core.store.attempts_for_repo("acme/alpha")[-1]
        self.assertEqual(updated["implementer_activity_at"], baseline + 901)
        self.assertEqual(updated["progress_deadline_at"], baseline + 901 + 1800)
        self.assertEqual(self.workflow.observe("acme/alpha", 7).coordination_state, "agent-working")

    def test_unknown_timeout_moves_to_infra_blocked_without_second_launch(self):
        self.start(LaunchDisposition.UNKNOWN)
        row = self.core.store.attempts_for_repo("acme/alpha")[-1]
        result = self.core.reconcile_once("acme/alpha", "owner", row["deadline_at"] + 1)
        self.assertEqual(result["items"][0]["terminal"], "infra-blocked")
        self.assertEqual(self.workflow.observe("acme/alpha", 7).coordination_state, "infra-blocked")
        self.assertEqual(self.core.store.attempt(row["attempt_id"])["phase"], "TERMINAL_UNRESOLVED")
        self.assertEqual(len(self.implementer.requests), 1)

    def test_claim_and_launch_phase_timeout_fail_closed_without_launch(self):
        created, _ = self.core.store.create_initial_attempt("acme/alpha", 7, "owner", 30, 60)
        attempt_id, _ = created
        self.set_snapshot(replace(self.workflow.observe("acme/alpha", 7), coordination_state="agent-working"))
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute("UPDATE attempts SET phase='LAUNCH_INTENT',deadline_at=10 WHERE attempt_id=?", (attempt_id,))
            conn.commit()
        result = self.core.reconcile_once("acme/alpha", "owner", 11)
        self.assertEqual(result["items"][0]["status"], "recovery_timeout")
        self.assertEqual(self.workflow.observe("acme/alpha", 7).coordination_state, "infra-blocked")
        self.assertFalse(self.implementer.requests)

    def test_unapplied_claim_becomes_retry_eligible_but_reconcile_does_not_launch(self):
        self.core.store.create_initial_attempt("acme/alpha", 7, "owner", 30, 60)
        result = self.core.reconcile_once("acme/alpha", "owner")
        self.assertEqual(result["items"][0]["status"], "retry_eligible")
        self.assertFalse(self.implementer.requests)
        self.assertEqual(self.core.store.attempts_for_repo("acme/alpha")[-1]["phase"], "RETRY_ELIGIBLE")

    def test_agent_ready_with_canonical_pr_contradicts_claim_and_fails_closed(self):
        self.core.store.create_initial_attempt("acme/alpha", 7, "owner", 30, 60)
        self.set_snapshot(replace(self.workflow.observe("acme/alpha", 7), pr_number=44, pr_open=True,
                                  pr_linked_issue=7, pr_head_sha=SHA, pr_branch=BRANCH,
                                  open_linked_pr_count=1, canonical_link_count=1))
        result = self.core.reconcile_once("acme/alpha", "owner")
        self.assertEqual(result["items"][0]["status"], "contradictory_claim_evidence")
        self.assertEqual(self.workflow.observe("acme/alpha", 7).coordination_state, "needs-human")
        self.assertFalse(self.implementer.requests)

    def test_same_owner_resumes_and_renews_existing_lease(self):
        self.start(LaunchDisposition.UNKNOWN)
        before = self.core.store.attempts_for_repo("acme/alpha")[0]
        self.core.reconcile_once("acme/alpha", "owner", time.time() + 5)
        with closing(sqlite3.connect(self.db)) as conn:
            expiry = conn.execute("SELECT expires_at FROM leases WHERE repo='acme/alpha'").fetchone()[0]
        self.assertGreater(expiry, time.time() + 10)
        self.assertEqual(self.core.store.attempts_for_repo("acme/alpha")[0]["deadline_at"], before["deadline_at"])

    def test_different_owner_cannot_steal_expired_lease(self):
        self.start(LaunchDisposition.UNKNOWN)
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute("UPDATE leases SET expires_at=0")
            conn.commit()
        transitions = list(self.workflow.transitions)
        result = self.core.reconcile_once("acme/alpha", "other-owner", time.time() + 100)
        self.assertEqual(result["items"][0]["status"], "owner_mismatch")
        self.assertEqual(self.workflow.transitions, transitions)
        self.assertEqual(len(self.implementer.requests), 1)

    def test_orphan_agent_working_with_one_canonical_pr_recovers_trust(self):
        self.set_snapshot(replace(snap(state="agent-working"), pr_number=44, pr_open=True,
                                  pr_linked_issue=7, pr_head_sha=SHA, pr_branch=BRANCH,
                                  open_linked_pr_count=1, canonical_link_count=1))
        result = self.core.reconcile_once("acme/alpha", "owner")
        self.assertEqual(result["items"][0]["status"], "orphan_adopted_trusted")
        self.assertTrue(self.core.store.flow("acme/alpha", 7)["trusted"])
        self.assertFalse(self.implementer.requests)

    def test_missing_attempt_ledger_recovers_trust_from_canonical_pr(self):
        self.start()
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute("DELETE FROM attempts")
            conn.commit()
        self.add_pr()
        result = self.core.reconcile_once("acme/alpha", "owner")
        self.assertEqual(result["items"][0]["status"], "orphan_adopted_trusted")
        self.assertTrue(self.core.store.flow("acme/alpha", 7)["trusted"])
        self.assertEqual(self.core.store.attempts_for_repo("acme/alpha")[-1]["kind"], "adopted")

    def test_canonical_adoption_without_budget_evidence_cannot_repair(self):
        self.set_snapshot(replace(snap(state="agent-working"), pr_number=44, pr_open=True,
                                  pr_linked_issue=7, pr_head_sha=SHA, pr_branch=BRANCH,
                                  open_linked_pr_count=1, canonical_link_count=1))
        self.core.reconcile_once("acme/alpha", "owner")
        candidate = RepairCandidate("acme/alpha", 7, 44, SHA, BRANCH,
                                    "implementation_failure", "check-1", "owner")
        result = self.core.dispatch_repair(candidate)
        self.assertEqual(result["status"], "repair_budget_unknown")
        self.assertEqual(result["reason"], "repair_history_or_budget_provenance_unknown")
        self.assertTrue(result["needs_human_transitioned"])
        self.assertEqual(self.workflow.observe("acme/alpha", 7).coordination_state, "needs-human")
        self.assertFalse(self.implementer.requests)

    def test_canonical_adoption_preserves_durable_repair_count_and_uses_next_ordinal(self):
        self.set_snapshot(replace(snap(state="agent-working"), pr_number=44, pr_open=True,
                                  pr_linked_issue=7, pr_head_sha=SHA, pr_branch=BRANCH,
                                  open_linked_pr_count=1, canonical_link_count=1))
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute("INSERT INTO flows(repo,issue_number,trusted,initial_confirmed,pr_number,branch,repair_count,"
                         "repair_budget_known) VALUES(?,?,?,?,?,?,?,?)",
                         ("acme/alpha", 7, 1, 1, 44, BRANCH, 2, 1))
            conn.commit()
        result = self.core.reconcile_once("acme/alpha", "owner")
        self.assertEqual(result["items"][0]["status"], "orphan_adopted_trusted")
        flow = self.core.store.flow("acme/alpha", 7)
        self.assertEqual((flow["repair_count"], flow["repair_budget_known"]), (2, 1))
        candidate = RepairCandidate("acme/alpha", 7, 44, SHA, BRANCH,
                                    "implementation_failure", "check-1", "owner")
        repair = self.core.dispatch_repair(candidate)
        self.assertEqual((repair["status"], repair["ordinal"]), ("launched", 3))
        self.assertEqual(self.core.store.flow("acme/alpha", 7)["repair_count"], 3)

    def test_legacy_flow_without_count_or_attempt_evidence_migrates_as_unknown(self):
        legacy_db = Path(self.temp.name) / "legacy.sqlite"
        with closing(sqlite3.connect(legacy_db)) as conn:
            conn.execute("CREATE TABLE flows(repo TEXT NOT NULL,issue_number INTEGER NOT NULL,"
                         "trusted INTEGER NOT NULL,initial_confirmed INTEGER NOT NULL,pr_number INTEGER,"
                         "branch TEXT,PRIMARY KEY(repo,issue_number))")
            conn.execute("INSERT INTO flows VALUES('acme/alpha',7,1,1,44,?)", (BRANCH,))
            conn.commit()
        core = RuntimeCore(legacy_db, MANIFEST, self.workflow, self.implementer)
        flow = core.store.flow("acme/alpha", 7)
        self.assertEqual((flow["repair_count"], flow["repair_budget_known"]), (0, 0))

    def test_orphan_without_pr_fails_closed_to_infra_blocked(self):
        self.set_snapshot(snap(state="agent-working"))
        result = self.core.reconcile_once("acme/alpha", "owner")
        self.assertEqual(result["items"][0]["status"], "orphan_without_pr")
        self.assertEqual(self.workflow.observe("acme/alpha", 7).coordination_state, "infra-blocked")

    def test_orphan_with_ambiguous_prs_fails_closed_to_needs_human(self):
        self.set_snapshot(replace(snap(state="agent-working"), open_linked_pr_count=2,
                                  canonical_link_count=2))
        result = self.core.reconcile_once("acme/alpha", "owner")
        self.assertEqual(result["items"][0]["status"], "ambiguous_pr")
        self.assertEqual(self.workflow.observe("acme/alpha", 7).coordination_state, "needs-human")

    def test_orphan_changes_requested_without_repair_history_needs_human(self):
        self.set_snapshot(snap(state="changes-requested"))
        result = self.core.reconcile_once("acme/alpha", "owner")
        self.assertEqual(result["items"][0]["status"], "untrusted_repair_history")
        self.assertEqual(self.workflow.observe("acme/alpha", 7).coordination_state, "needs-human")

    def test_deleted_repair_ledger_never_becomes_zero_repair_authority(self):
        self.start()
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute("DELETE FROM attempts WHERE kind='repair'")
            conn.commit()
        self.set_snapshot(snap(state="changes-requested"))
        result = self.core.reconcile_once("acme/alpha", "owner")
        self.assertEqual(result["items"][0]["status"], "untrusted_repair_history")
        self.assertEqual(self.workflow.observe("acme/alpha", 7).coordination_state, "needs-human")
        self.assertEqual(len(self.implementer.requests), 1)

    def test_deleted_repair_rows_do_not_reset_durable_repair_budget(self):
        self.start()
        for ordinal in range(1, 4):
            head = f"{ordinal:040x}"
            self.add_pr(head=head)
            candidate = RepairCandidate("acme/alpha", 7, 44, head, BRANCH,
                                        "implementation_failure", f"check-{ordinal}", "owner")
            self.assertEqual(self.core.dispatch_repair(candidate)["status"], "launched")
        self.assertEqual(self.core.store.flow("acme/alpha", 7)["repair_count"], 3)
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute("DELETE FROM attempts WHERE kind='repair'")
            conn.commit()
        self.assertEqual(self.core.store.flow("acme/alpha", 7)["repair_count"], 3)
        fourth_head = f"{3:040x}"
        self.add_pr(head=f"{3:040x}")
        candidate = RepairCandidate("acme/alpha", 7, 44, fourth_head, BRANCH,
                                    "implementation_failure", "check-4", "owner")
        self.assertEqual(self.core.dispatch_repair(candidate)["status"], "budget_exhausted")
        self.assertEqual(len(self.implementer.requests), 4)
        self.assertEqual(self.workflow.observe("acme/alpha", 7).coordination_state, "needs-human")

    def test_issue_closure_during_active_work_stops_locally(self):
        self.start(LaunchDisposition.UNKNOWN)
        self.set_snapshot(replace(self.workflow.observe("acme/alpha", 7), issue_open=False))
        count = len(self.workflow.transitions)
        self.core.reconcile_once("acme/alpha", "owner")
        row = self.core.store.attempts_for_repo("acme/alpha")[-1]
        self.assertEqual(row["phase"], "STOPPED_CANCELLED")
        self.assertEqual(len(self.workflow.transitions), count)
        self.assertEqual(len(self.implementer.requests), 1)

    def test_frozen_spec_removal_during_active_work_stops_locally(self):
        self.start(LaunchDisposition.UNKNOWN)
        self.set_snapshot(replace(self.workflow.observe("acme/alpha", 7), frozen_spec=False))
        self.core.reconcile_once("acme/alpha", "owner")
        self.assertEqual(self.core.store.attempts_for_repo("acme/alpha")[-1]["phase"], "STOPPED_CANCELLED")
        self.assertEqual(len(self.implementer.requests), 1)

    def test_terminal_label_during_active_work_is_never_cleared(self):
        self.start(LaunchDisposition.UNKNOWN)
        self.set_snapshot(replace(self.workflow.observe("acme/alpha", 7), coordination_state="needs-human",
                                  terminal_labels=("needs-human",)))
        transitions = list(self.workflow.transitions)
        self.core.reconcile_once("acme/alpha", "owner")
        self.assertEqual(self.workflow.observe("acme/alpha", 7).terminal_labels, ("needs-human",))
        self.assertEqual(self.workflow.transitions, transitions)
        self.assertEqual(self.core.store.attempts_for_repo("acme/alpha")[-1]["phase"], "STOPPED_CANCELLED")

    def test_late_event_cannot_revive_locally_cancelled_flow(self):
        self.start(LaunchDisposition.UNKNOWN)
        self.set_snapshot(replace(self.workflow.observe("acme/alpha", 7), issue_open=False))
        self.core.reconcile_once("acme/alpha", "owner")
        self.set_snapshot(snap(state="agent-working"))
        transitions = list(self.workflow.transitions)
        result = self.core.reconcile_once("acme/alpha", "owner")
        self.assertEqual(result["items"][0]["status"], "stopped_terminal")
        self.assertEqual(self.workflow.transitions, transitions)
        self.assertEqual(len(self.implementer.requests), 1)

    def test_deadline_survives_restart_observation_and_lease_renewal(self):
        self.start(LaunchDisposition.UNKNOWN)
        before = self.core.store.attempts_for_repo("acme/alpha")[0]["deadline_at"]
        restarted = RuntimeCore(self.db, MANIFEST, self.workflow, self.implementer, recovery_timeout_seconds=60)
        restarted.reconcile_once("acme/alpha", "owner", time.time() + 1)
        restarted.reconcile_once("acme/alpha", "owner", time.time() + 2)
        after = restarted.store.attempts_for_repo("acme/alpha")[0]["deadline_at"]
        self.assertEqual(before, after)

    def test_meaningful_pr_binding_advances_durable_phase_time(self):
        self.start(LaunchDisposition.UNKNOWN)
        old = self.core.store.attempts_for_repo("acme/alpha")[0]["phase_entered_at"]
        self.add_pr()
        now = time.time() + 2
        self.core.reconcile_once("acme/alpha", "owner", now)
        bound = self.core.store.attempts_for_repo("acme/alpha")[0]
        self.assertEqual(bound["phase"], "PR_BOUND")
        self.assertEqual(bound["phase_entered_at"], now)
        self.assertGreater(bound["phase_entered_at"], old)

    def _bind_initial_pr(self):
        self.start(LaunchDisposition.UNKNOWN)
        self.add_pr()
        result = self.core.reconcile_once("acme/alpha", "owner")
        self.assertEqual(result["items"][0]["status"], "pr_bound")
        row = self.core.store.attempts_for_repo("acme/alpha")[-1]
        self.assertEqual(row["phase"], "PR_BOUND")
        return row

    def _assert_pr_bound_cancelled(self):
        row = self.core.store.attempts_for_repo("acme/alpha")[-1]
        self.assertEqual(row["phase"], "STOPPED_CANCELLED")
        self.assertFalse(self.core.store.flow("acme/alpha", 7)["trusted"])
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertIsNone(conn.execute("SELECT 1 FROM leases WHERE repo='acme/alpha' AND issue_number=7").fetchone())
        self.assertEqual(len(self.implementer.requests), 1)

    def test_pr_bound_issue_closed_is_fenced_and_lease_released(self):
        self._bind_initial_pr()
        self.set_snapshot(replace(self.workflow.observe("acme/alpha", 7), issue_open=False))
        self.core.reconcile_once("acme/alpha", "owner")
        self._assert_pr_bound_cancelled()

    def test_pr_bound_frozen_spec_removed_is_fenced_and_lease_released(self):
        self._bind_initial_pr()
        self.set_snapshot(replace(self.workflow.observe("acme/alpha", 7), frozen_spec=False))
        self.core.reconcile_once("acme/alpha", "owner")
        self._assert_pr_bound_cancelled()

    def test_pr_bound_terminal_label_is_preserved_and_flow_stopped(self):
        self._bind_initial_pr()
        self.set_snapshot(replace(self.workflow.observe("acme/alpha", 7), coordination_state="needs-human",
                                  terminal_labels=("needs-human",)))
        transitions = list(self.workflow.transitions)
        self.core.reconcile_once("acme/alpha", "owner")
        self.assertEqual(self.workflow.observe("acme/alpha", 7).terminal_labels, ("needs-human",))
        self.assertEqual(self.workflow.transitions, transitions)
        self._assert_pr_bound_cancelled()

    def test_stale_event_after_pr_bound_cancellation_does_not_restore_trust_or_phase(self):
        self._bind_initial_pr()
        self.set_snapshot(replace(self.workflow.observe("acme/alpha", 7), issue_open=False))
        self.core.reconcile_once("acme/alpha", "owner")
        self.set_snapshot(replace(snap(state="agent-working"), pr_number=44, pr_open=True, pr_linked_issue=7,
                                  pr_head_sha=SHA, pr_branch=BRANCH, open_linked_pr_count=1,
                                  canonical_link_count=1))
        result = self.core.reconcile_once("acme/alpha", "owner")
        self.assertEqual(result["items"][0]["status"], "stopped_terminal")
        self.assertEqual(self.core.store.attempts_for_repo("acme/alpha")[-1]["phase"], "STOPPED_CANCELLED")
        self.assertFalse(self.core.store.flow("acme/alpha", 7)["trusted"])
        self.assertEqual(len(self.implementer.requests), 1)

    def test_stale_authorization_during_recovery_cannot_bind_pr(self):
        self.start(LaunchDisposition.UNKNOWN)
        self.add_pr()
        self.workflow.observe_count = 0
        def revoke_on_second_read(workflow, repo, issue, count):
            if count == 2:
                current = workflow.states[(repo.casefold(), issue)]
                workflow.states[(repo.casefold(), issue)] = replace(current, revision="revoked", frozen_spec=False)
        self.workflow.observe_hook = revoke_on_second_read
        result = self.core.reconcile_once("acme/alpha", "owner")
        self.assertEqual(result["items"][0]["status"], "stale_recovery_evidence")
        self.assertIsNone(self.core.store.flow("acme/alpha", 7)["pr_number"])
        self.assertEqual(len(self.implementer.requests), 1)

    def test_agent_ready_is_not_discovered_or_claimed_by_recovery(self):
        result = self.core.reconcile_once("acme/alpha", "owner")
        self.assertEqual(result["items"], [])
        self.assertEqual(self.workflow.observe("acme/alpha", 7).coordination_state, "agent-ready")
        self.assertFalse(self.implementer.requests)

    def test_two_consumers_in_shared_db_remain_isolated_during_reconcile(self):
        self.start()
        self.workflow.states[("other/repo", 7)] = snap("other/repo")
        other = RuntimeCore(self.db, MANIFEST, self.workflow, self.implementer)
        self.assertEqual(other.dispatch_initial("other/repo", 7, "owner")["status"], "launched")
        self.workflow.states[("other/repo", 7)] = replace(self.workflow.observe("other/repo", 7),
                                                           coordination_state="agent-working")
        self.workflow.states[("acme/alpha", 7)] = replace(self.workflow.observe("acme/alpha", 7),
            pr_number=44, pr_open=True, pr_linked_issue=7, pr_head_sha=SHA, pr_branch=BRANCH,
            open_linked_pr_count=1, canonical_link_count=1)
        self.core.reconcile_once("acme/alpha", "owner")
        self.assertEqual(self.core.store.flow("acme/alpha", 7)["pr_number"], 44)
        self.assertIsNone(other.store.flow("other/repo", 7)["pr_number"])
        self.assertEqual(other.store.attempts_for_repo("other/repo")[-1]["phase"], "LAUNCH_CONFIRMED")
        self.assertEqual(len(self.implementer.requests), 2)


if __name__ == "__main__":
    unittest.main()
