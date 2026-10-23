"""Terminal stop visibility, evidence-gap routing, and exact-head reviewer resume."""
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from vccp_runtime.adapters import generic_prompt
from vccp_runtime import runner
from vccp_runtime.core import LaunchDisposition, LaunchResult, RuntimeCore, WorkflowSnapshot
from vccp_runtime.evidence import (evaluate_runtime_evidence, parse_runtime_evidence,
                                   render_evidence_section)
from vccp_runtime.lifecycle import LifecycleDriver
from vccp_runtime.local_reviewer import LocalReviewerDriver, LocalReviewerWriter
from vccp_runtime.reviewer_agents import _ReviewerTask


REPO = "acme/alpha"
ISSUE = 7
PR = 44
HEAD = "a" * 40
HEAD_B = "b" * 40
PROVIDER = "test_provider"
MANIFEST = {
    "schema_version": 2,
    "repository": {"base_branch": "main", "implementer_authors": ["builder[bot]"]},
    "issue_contract": {"max_automated_repairs": 3,
                       "active_coordination_labels": ["agent-ready", "agent-working",
                                                      "changes-requested"],
                       "terminal_coordination_labels": ["infra-blocked", "needs-human"]},
    "reviewer": {"required_checks": [{"name": "Unit", "accepted_conclusions": ["success"],
                                      "implementation_failure_conclusions": ["failure"],
                                      "infrastructure_failure_conclusions": ["timed_out"]}]},
    "merge": {"method": "squash"},
}


def snapshot(**changes):
    values = dict(repo=REPO, issue_number=ISSUE, revision="r1", issue_open=True,
                  coordination_state="agent-working", frozen_spec=True, terminal_labels=(),
                  canonical_relationship_valid=True, canonical_link_count=1,
                  open_linked_pr_count=1, pr_number=PR, pr_open=True, pr_state="open",
                  pr_linked_issue=ISSUE, pr_head_sha=HEAD, pr_branch="implement/7",
                  active_labels=("agent-working",),
                  check_runs=({"name": "Unit", "head_sha": HEAD, "status": "completed",
                               "conclusion": "success", "id": 1},),
                  frozen_spec_body="Frozen spec.")
    values.update(changes)
    return WorkflowSnapshot(**values)


class FakeWorkflow:
    """Mutable GitHub stand-in; label transitions really change the observed snapshot."""

    def __init__(self, initial=None):
        self.snapshots = {(REPO, ISSUE): initial or snapshot()}
        self.posted = []
        self.transitions = []
        self.restored = []
        self.fail_publish = 0

    def observe(self, repo, issue):
        return self.snapshots[(repo, issue)]

    def transition_coordination_state(self, repo, issue, expected_state, new_state,
                                      expected_revision):
        current = self.snapshots[(repo, issue)]
        if current.revision != expected_revision or current.coordination_state != expected_state:
            return False
        terminal = (new_state,) if new_state in {"infra-blocked", "needs-human"} else ()
        self.snapshots[(repo, issue)] = replace(
            current, revision=current.revision + "+", coordination_state=new_state,
            terminal_labels=terminal,
            active_labels=() if terminal else (new_state,))
        self.transitions.append((repo, issue, expected_state, new_state))
        return True

    def restore_repair_coordination_state(self, repo, issue, new_state, expected_revision):
        current = self.snapshots[(repo, issue)]
        if current.terminal_labels != ("needs-human",):
            return False
        self.snapshots[(repo, issue)] = replace(
            current, revision=current.revision + "+", coordination_state=new_state,
            terminal_labels=(), active_labels=(new_state,))
        self.restored.append((repo, issue, new_state))
        return True

    def post_terminal_stop_summary(self, repo, issue_number, label, reason, summary="",
                                   pr_number=None, head_sha=None):
        if self.fail_publish:
            self.fail_publish -= 1
            return None
        self.posted.append({"repo": repo, "issue": issue_number, "label": label,
                            "reason": reason, "summary": summary,
                            "pr": pr_number, "head": head_sha})
        return 1000 + len(self.posted)

    def clear_active_coordination_labels(self, repo, issue):
        return True

    def merged_closing_pr(self, repo, issue, pr_number):
        return None


class FakeImplementer:
    def __init__(self):
        self.launches = []

    def launch(self, request):
        self.launches.append(request)
        return LaunchResult(LaunchDisposition.CONFIRMED, f"exec-{len(self.launches)}", PROVIDER)

    def latest_activity_for(self, provider, execution_id):
        return None


class FakeReviewer:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def advance(self, repo, issue, pr, head):
        self.calls.append((repo, issue, pr, head))
        return {**self.result, "head_sha": head}


class LifecycleCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "runtime.sqlite"
        self.workflow = FakeWorkflow(snapshot(coordination_state="agent-ready",
                                              open_linked_pr_count=0,
                                              pr_number=None, pr_open=False,
                                              pr_head_sha=None, pr_branch=None,
                                              active_labels=()))
        self.implementer = FakeImplementer()
        self.core = RuntimeCore(self.db, MANIFEST, self.workflow, self.implementer,
                                lease_ttl_seconds=300, recovery_timeout_seconds=900)
        self.assertEqual(self.core.dispatch_initial(REPO, ISSUE, "owner")["status"], "launched")
        attempt = self.core.store.attempts_for_repo(REPO)[0]
        self.assertTrue(self.core.store.bind_pr(attempt["attempt_id"], PR, "implement/7", HEAD, 1))
        self.workflow.snapshots[(REPO, ISSUE)] = snapshot()

    def driver(self, reviewer=None):
        return LifecycleDriver(self.core, MANIFEST,
                               {"waiting_ci": 3600, "waiting_review": 3600, "waiting_merge": 3600},
                               local_reviewer=reviewer)


class TerminalReasonTests(LifecycleCase):
    def test_human_evidence_gap_posts_exactly_one_reason_comment(self):
        reviewer = FakeReviewer({"status": "reviewer_insufficient_evidence",
                                 "summary": "Criterion 2 needs a human deployment choice.",
                                 "remediation": "human"})

        first = self.driver(reviewer).advance_once(REPO, "owner", 100)

        self.assertEqual(first["items"][0]["status"], "needs-human")
        self.assertEqual(len(self.workflow.posted), 1)
        posted = self.workflow.posted[0]
        self.assertEqual((posted["repo"], posted["issue"], posted["label"]),
                         (REPO, ISSUE, "needs-human"))
        self.assertEqual(posted["reason"], "reviewer_insufficient_evidence")
        self.assertIn("human deployment choice", posted["summary"])
        self.assertEqual((posted["pr"], posted["head"]), (PR, HEAD))

        # Same process, a later pass, and a fresh driver all observe the durable stop.
        self.driver(reviewer).advance_once(REPO, "owner", 101)
        LifecycleDriver(self.core, MANIFEST, local_reviewer=reviewer).advance_once(
            REPO, "owner", 102)
        self.assertEqual(len(self.workflow.posted), 1)

    def test_infra_blocked_stop_also_leaves_one_reason_comment(self):
        self.workflow.snapshots[(REPO, ISSUE)] = snapshot(
            check_runs=({"name": "Unit", "head_sha": HEAD, "status": "completed",
                         "conclusion": "timed_out", "id": 3},))
        # Two infrastructure retries are allowed before the terminal stop.
        self.driver().advance_once(REPO, "owner", 100)
        self.driver().advance_once(REPO, "owner", 101)
        result = self.driver().advance_once(REPO, "owner", 102)

        self.assertEqual(result["items"][0]["status"], "infra-blocked")
        self.assertEqual(len(self.workflow.posted), 1)
        self.assertEqual(self.workflow.posted[0]["label"], "infra-blocked")
        self.assertEqual(self.workflow.posted[0]["reason"], "explicit_infrastructure_failure")

    def test_notice_ledger_prevents_a_restart_from_duplicating_the_comment(self):
        first = self.core.publish_terminal_stop(REPO, ISSUE, "needs-human",
                                                "reviewer_insufficient_evidence", "missing evidence",
                                                PR, HEAD, 100)
        second = self.core.publish_terminal_stop(REPO, ISSUE, "needs-human",
                                                 "reviewer_insufficient_evidence", "missing evidence",
                                                 PR, HEAD, 101)
        self.assertEqual((first, second), (1001, None))
        self.assertEqual(len(self.workflow.posted), 1)
        notice = self.core.store.terminal_notice(REPO, ISSUE, "needs-human",
                                                 "reviewer_insufficient_evidence", HEAD)
        self.assertEqual(notice["comment_id"], 1001)

    def test_failed_comment_write_is_retried_once_and_then_recorded(self):
        self.workflow.fail_publish = 1

        self.assertIsNone(self.core.publish_terminal_stop(
            REPO, ISSUE, "needs-human", "reviewer_insufficient_evidence", "missing evidence",
            PR, HEAD, 100))
        self.assertIsNone(self.core.store.terminal_notice(
            REPO, ISSUE, "needs-human", "reviewer_insufficient_evidence", HEAD))
        self.assertEqual(self.core.publish_terminal_stop(
            REPO, ISSUE, "needs-human", "reviewer_insufficient_evidence", "missing evidence",
            PR, HEAD, 101), 1001)
        self.assertEqual(len(self.workflow.posted), 1)


class EvidenceGapRoutingTests(LifecycleCase):
    def _blocked_execution(self, remediation):
        self.core.store.claim_reviewer_execution(REPO, PR, HEAD, PROVIDER, now=50)
        self.core.store.update_reviewer_execution(
            REPO, PR, HEAD, "COMPLETED", "exec",
            outcome=json.dumps({"event": "BLOCKED", "body": "Missing runtime evidence.",
                                "remediation": remediation}), now=51)

    def test_agent_gap_returns_to_the_same_pr_and_never_stops_for_a_human(self):
        self._blocked_execution("agent")
        reviewer = FakeReviewer({"status": "reviewer_insufficient_evidence",
                                 "summary": "The deploy step authorized by the Frozen Spec was not run.",
                                 "remediation": "agent"})

        result = self.driver(reviewer).advance_once(REPO, "owner", 100)

        item = result["items"][0]
        self.assertEqual(item["trigger"], "evidence_remediation")
        self.assertEqual(item["status"], "launched")
        self.assertNotIn("needs-human", [entry[3] for entry in self.workflow.transitions])
        self.assertEqual(self.workflow.posted, [])
        launch = self.implementer.launches[-1]
        self.assertEqual((launch.attempt_kind, launch.repair_cause_type),
                         ("repair", "evidence_remediation"))
        self.assertEqual((launch.pr_number, launch.expected_head_sha), (PR, HEAD))
        self.assertEqual(launch.review_feedback,
                         "The deploy step authorized by the Frozen Spec was not run.")
        prompt = generic_prompt(launch, "/workspace", MANIFEST, "app-gh", "app-git-push")
        self.assertIn("This is not a code defect", prompt)
        self.assertIn("vccp-runtime-evidence", prompt)
        self.assertNotIn("blocking review below", prompt)
        self.assertEqual(self.core.store.reviewer_execution(REPO, PR, HEAD)["state"], "COMPLETED")

    def test_agent_gap_without_a_durable_blocked_execution_still_stops_for_a_human(self):
        # A Reviewer that reports a gap but left no durable BLOCKED cause cannot authorize
        # work; the stop becomes visible instead of silently looping.
        reviewer = FakeReviewer({"status": "reviewer_insufficient_evidence",
                                 "summary": "missing evidence", "remediation": "agent"})

        result = self.driver(reviewer).advance_once(REPO, "owner", 100)

        self.assertEqual(result["items"][0]["status"], "needs-human")
        self.assertEqual(len(self.workflow.posted), 1)

    def test_human_gap_stops_and_never_dispatches_the_implementer(self):
        self._blocked_execution("human")
        reviewer = FakeReviewer({"status": "reviewer_insufficient_evidence",
                                 "summary": "Needs a maintainer credential.",
                                 "remediation": "human"})
        launches_before = len(self.implementer.launches)

        result = self.driver(reviewer).advance_once(REPO, "owner", 100)

        self.assertEqual(result["items"][0]["status"], "needs-human")
        self.assertEqual(len(self.implementer.launches), launches_before)
        self.assertEqual(len(self.workflow.posted), 1)
        self.assertIn("maintainer credential", self.workflow.posted[0]["summary"])

    def test_a_report_claiming_agent_cannot_override_a_human_durable_cause(self):
        # The durable BLOCKED conclusion is the cause record. A later report that claims
        # the gap is agent-fixable must not turn a human-only stop into automated work.
        self._blocked_execution("human")
        reviewer = FakeReviewer({"status": "reviewer_insufficient_evidence",
                                 "summary": "The implementer could supply this.",
                                 "remediation": "agent"})
        launches_before = len(self.implementer.launches)

        result = self.driver(reviewer).advance_once(REPO, "owner", 100)

        self.assertEqual(result["items"][0]["status"], "needs-human")
        self.assertEqual(len(self.implementer.launches), launches_before)
        self.assertEqual(self.core.store.reviewer_execution(REPO, PR, HEAD)["state"], "COMPLETED")

    def test_unclassified_gap_is_not_guessed_as_agent_work(self):
        # Even with a durable agent-classified cause at this head, a decision that arrives
        # without its own class is not promoted into automated work.
        self._blocked_execution("agent")
        reviewer = FakeReviewer({"status": "reviewer_insufficient_evidence",
                                 "summary": "unclear owner"})

        result = self.driver(reviewer).advance_once(REPO, "owner", 100)

        self.assertEqual(result["items"][0]["status"], "needs-human")
        self.assertEqual(len(self.implementer.launches), 1)
        self.assertEqual(len(self.workflow.posted), 1)


class FakeReviewAgent:
    def __init__(self, execution=None):
        self.launches = []
        self.execution = execution or {"execution_id": "review-session-2"}

    def launch_review(self, repo, issue, pr, head):
        self.launches.append((repo, issue, pr, head))
        return dict(self.execution)


class FakeGitHubProvider:
    target_repository = REPO

    def __init__(self, head=HEAD):
        self.head = head
        self.calls = []

    def request(self, method, path, repo, payload=None):
        self.calls.append((method, path))
        if path.endswith(f"/pulls/{PR}"):
            return 200, {"state": "open", "draft": False, "head": {"sha": self.head},
                         "base": {"ref": "main"}, "user": {"login": "builder[bot]"},
                         "node_id": "node"}
        if f"/pulls/{PR}/reviews?" in path:
            return 200, []
        raise AssertionError((method, path))

    def verify_identity(self):
        return {"login": "liuchangchxy", "verified": True}


class ReviewerResumeTests(LifecycleCase):
    def _stopped_fixture(self):
        """A real needs-human stop with a durable BLOCKED Reviewer conclusion at HEAD."""
        attempt = self.core.store.attempts_for_repo(REPO)[0]
        self.core.store.local_terminal(attempt["attempt_id"], "TERMINAL_UNRESOLVED", 50)
        self.core.store.set_lifecycle(REPO, ISSUE, "NEEDS_HUMAN", HEAD, 51, 1,
                                      "reviewer_insufficient_evidence: missing browser evidence")
        self.core.store.claim_reviewer_execution(REPO, PR, HEAD, PROVIDER, now=52)
        self.core.store.update_reviewer_execution(
            REPO, PR, HEAD, "COMPLETED", "review-session-1",
            outcome=json.dumps({"event": "BLOCKED", "body": "missing browser evidence",
                                "remediation": "human"}), now=53)
        self.workflow.snapshots[(REPO, ISSUE)] = snapshot(
            coordination_state="needs-human", terminal_labels=("needs-human",),
            active_labels=())
        return LocalReviewerDriver(
            self.core.store, FakeReviewAgent(),
            LocalReviewerWriter(FakeGitHubProvider(), self.workflow, REPO,
                                "liuchangchxy", MANIFEST),
            PROVIDER)

    def test_human_authorization_reruns_the_reviewer_at_the_same_head(self):
        driver = self._stopped_fixture()

        result = driver.resume_after_evidence(REPO, ISSUE, PR, HEAD,
                                              authorization_head=HEAD)

        self.assertEqual(result["status"], "reviewer_running")
        self.assertEqual(driver.provider.launches, [(REPO, ISSUE, PR, HEAD)])
        record = self.core.store.reviewer_execution(REPO, PR, HEAD)
        self.assertEqual((record["state"], record["execution_id"]), ("RUNNING", "review-session-2"))
        self.assertEqual(self.core.store.attempts_for_repo(REPO)[0]["phase"], "PR_BOUND")
        flow = self.core.store.flow(REPO, ISSUE)
        self.assertEqual((flow["trusted"], flow["initial_confirmed"]), (1, 1))
        history = self.core.store.evidence_resume_history(REPO, ISSUE, PR)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["authorized_by"], "liuchangchxy")
        self.assertIsNotNone(history[0]["consumed_at"])
        # The Implementer is never restarted and no new dispatch is recorded.
        self.assertEqual(len(self.implementer.launches), 1)

    def test_a_consumed_authorization_cannot_be_replayed(self):
        driver = self._stopped_fixture()
        self.assertEqual(driver.resume_after_evidence(REPO, ISSUE, PR, HEAD,
                                                      authorization_head=HEAD)["status"],
                         "reviewer_running")
        # A second invocation must not silently re-arm the same head a second time.
        self.assertEqual(self.core.store.claim_authorized_evidence_resume(
            REPO, PR, HEAD, PROVIDER), (None, False))
        self.assertEqual(len(driver.provider.launches), 1)
        self.assertEqual(len(self.core.store.evidence_resume_history(REPO, ISSUE, PR)), 1)

    def test_changed_head_invalidates_the_old_authorization(self):
        driver = self._stopped_fixture()
        self.assertTrue(self.core.store.authorize_evidence_resume(
            REPO, ISSUE, PR, HEAD, "liuchangchxy", now=60)[0])

        self.assertEqual(self.core.store.claim_authorized_evidence_resume(
            REPO, PR, HEAD_B, PROVIDER), (None, False))
        rejected = driver.resume_after_evidence(REPO, ISSUE, PR, HEAD_B,
                                                authorization_head=HEAD_B)
        self.assertEqual(rejected["status"], "reviewer_resume_rejected")
        self.assertEqual(driver.provider.launches, [])
        # And a new head cannot borrow the old stop's authorization either.
        self.core.store.set_lifecycle(REPO, ISSUE, "NEEDS_HUMAN", HEAD_B, 61, 1,
                                      "reviewer_insufficient_evidence: still missing")
        self.assertEqual(self.core.store.authorize_evidence_resume(
            REPO, ISSUE, PR, HEAD_B, "liuchangchxy", now=62),
            (False, "reviewer_execution_not_blocked_at_this_head"))
        # The durable stop now belongs to the new head, so the old head's authorization
        # is refused even though a BLOCKED Reviewer conclusion still exists for it.
        self.assertEqual(self.core.store.authorize_evidence_resume(
            REPO, ISSUE, PR, HEAD, "liuchangchxy", now=63),
            (False, "not_an_evidence_stop_for_this_head"))

    def test_failed_exact_head_recheck_restores_the_terminal_fence(self):
        driver = self._stopped_fixture()
        # CI evidence is no longer acceptable at this head: the resumed review cannot run.
        self.workflow.snapshots[(REPO, ISSUE)] = replace(
            self.workflow.snapshots[(REPO, ISSUE)], check_runs=())

        result = driver.resume_after_evidence(REPO, ISSUE, PR, HEAD,
                                              authorization_head=HEAD)

        self.assertEqual(result["reason"], "exact_head_preconditions_changed")
        self.assertEqual(driver.provider.launches, [])
        after = self.workflow.observe(REPO, ISSUE)
        self.assertEqual(after.terminal_labels, ("needs-human",))
        self.assertEqual(after.coordination_state, "needs-human")
        # Consent survives for a later retry but was not consumed by a refused run.
        history = self.core.store.evidence_resume_history(REPO, ISSUE, PR)
        self.assertEqual(len(history), 1)
        self.assertIsNone(history[0]["consumed_at"])

    def test_resume_is_refused_without_a_matching_evidence_stop(self):
        driver = self._stopped_fixture()
        self.core.store.set_lifecycle(REPO, ISSUE, "NEEDS_HUMAN", HEAD, 61, 1,
                                      "ambiguous_ci_failure")

        result = driver.resume_after_evidence(REPO, ISSUE, PR, HEAD,
                                              authorization_head=HEAD)

        self.assertEqual(result["status"], "reviewer_resume_rejected")
        self.assertEqual(result["reason"], "not_an_evidence_stop_for_this_head")
        self.assertEqual(driver.provider.launches, [])

    def test_authorization_must_repeat_the_exact_head(self):
        driver = self._stopped_fixture()

        result = driver.resume_after_evidence(REPO, ISSUE, PR, HEAD,
                                              authorization_head=HEAD_B)

        self.assertEqual(result["reason"], "authorization_or_head_mismatch")
        self.assertEqual(driver.provider.launches, [])
        self.assertIsNone(self.core.store.reviewer_execution(REPO, PR, HEAD_B))


class RuntimeEvidenceTests(unittest.TestCase):
    def test_mismatched_tested_revision_never_satisfies_exact_head_acceptance(self):
        text = ("PR body\n```vccp-runtime-evidence\n"
                '{"expected_revision": "' + HEAD + '", '
                '"tested_revision": "' + HEAD_B + '", "target": "fnos host A", '
                '"image_digest": "sha256:abc"}\n```\n')

        verdicts = evaluate_runtime_evidence(parse_runtime_evidence(text), HEAD)

        self.assertEqual(len(verdicts), 1)
        self.assertFalse(verdicts[0]["accepted"])
        self.assertEqual(verdicts[0]["reason"], "tested_revision_differs_from_expected")
        rendered = render_evidence_section(verdicts, HEAD)
        self.assertIn("accepted=no", rendered)
        self.assertIn("tested_revision_differs_from_expected", rendered)
        self.assertIn("cannot satisfy that criterion", rendered)

    def test_matching_tested_revision_is_accepted_and_missing_fields_are_unusable(self):
        text = ("```vccp-runtime-evidence\n"
                '{"expected_revision": "' + HEAD + '", "tested_revision": "' + HEAD + '", '
                '"target": "fnos host A"}\n```\n'
                "```vccp-runtime-evidence\n"
                '{"expected_revision": "' + HEAD + '"}\n```\n'
                "```vccp-runtime-evidence\nnot json\n```\n")

        verdicts = evaluate_runtime_evidence(parse_runtime_evidence(text), HEAD)

        self.assertTrue(verdicts[0]["accepted"])
        self.assertFalse(verdicts[1]["accepted"])
        self.assertTrue(verdicts[1]["reason"].startswith("missing_"))
        self.assertEqual(verdicts[2]["reason"], "malformed_json")

    def test_reviewer_prompt_carries_the_verdicts_for_the_exact_head(self):
        api = SimpleNamespace(get=lambda path: (
            {"body": "Frozen spec."} if path.endswith(f"/issues/{ISSUE}") else
            {"number": PR, "title": "Change", "state": "open", "draft": False,
             "head": {"sha": HEAD}, "base": {"ref": "main"},
             "user": {"login": "builder[bot]"},
             "body": ('```vccp-runtime-evidence\n{"expected_revision": "' + HEAD_B
                      + '", "tested_revision": "' + HEAD_B + '", "target": "old host"}\n```')}
            if path.endswith(f"/pulls/{PR}") else
            [{"filename": "a.py", "status": "modified", "additions": 1, "deletions": 0,
              "patch": "+x"}] if f"/pulls/{PR}/files?" in path else []))
        with tempfile.TemporaryDirectory() as root:
            contract = Path(root) / "contract.md"
            contract.write_text("Review the exact head.", encoding="utf-8")
            task = _ReviewerTask(api, MANIFEST, contract,
                                 SimpleNamespace(prepare=lambda repo, pr, head: Path(root)))

            _, prompt = task.build(REPO, ISSUE, PR, HEAD)

        self.assertIn("<runtime-evidence>", prompt)
        self.assertIn("expected_revision_is_not_the_exact_head", prompt)
        self.assertIn("accepted=no", prompt)


class ResumeCommandTests(unittest.TestCase):
    def test_cli_requires_an_exact_head_confirmation_for_the_resume(self):
        base = ["--manifest", "m.yml", "--runtime-config", "r.json", "--repo", REPO,
                "--resume-review-after-evidence", "--issue", "7", "--pr", "44",
                "--head-sha", HEAD]
        for extra in ([], ["--authorize-evidence-resume-for", HEAD_B]):
            with self.subTest(extra=extra), self.assertRaises(SystemExit) as error:
                runner.main(base + extra)
            self.assertEqual(error.exception.code, 2)

    def test_confirmation_flag_is_rejected_outside_the_resume_mode(self):
        with self.assertRaises(SystemExit) as error:
            runner.main(["--manifest", "m.yml", "--runtime-config", "r.json", "--repo", REPO,
                         "--one-cycle", "--authorize-evidence-resume-for", HEAD])
        self.assertEqual(error.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
