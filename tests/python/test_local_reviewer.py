import unittest
import tempfile
from pathlib import Path

from vccp_runtime.core import WorkflowSnapshot, _Store
from vccp_runtime.local_reviewer import (GitHubUserReviewerProvider, LocalReviewerDriver,
                                         LocalReviewerWriter)


REPO = "owner/consumer"
HEAD = "a" * 40
MANIFEST = {
    "schema_version": 2,
    "repository": {"base_branch": "main", "implementer_authors": ["builder[bot]"]},
    "issue_contract": {
        "frozen_spec_label": "frozen-spec",
        "active_coordination_labels": ["agent-ready", "agent-working", "changes-requested"],
        "terminal_coordination_labels": ["infra-blocked", "needs-human"],
    },
    "reviewer": {"required_checks": [{"name": "Unit Tests", "accepted_conclusions": ["success"]}]},
    "merge": {"method": "squash"},
}


def snapshot(**changes):
    values = dict(repo=REPO, issue_number=7, revision="issue-revision", issue_open=True,
                  coordination_state="agent-working", frozen_spec=True, terminal_labels=(),
                  canonical_relationship_valid=True, canonical_link_count=1, pr_number=12,
                  pr_open=True, pr_linked_issue=7, pr_head_sha=HEAD, pr_state="open",
                  check_runs=({"name": "Unit Tests", "head_sha": HEAD, "status": "completed",
                               "conclusion": "success", "id": 1},))
    values.update(changes)
    return WorkflowSnapshot(**values)


class FakeProvider:
    api_url = "https://api.github.com"
    target_repository = REPO

    def __init__(self, head=HEAD, author="builder[bot]", checks=None, reviews=None):
        self.head, self.author = head, author
        self.checks = checks
        self.reviews = reviews or []
        self.calls = []
        self.labels = ["frozen-spec", "agent-working"]
        self.automerge = False

    def request(self, method, path, repo, payload=None):
        self.calls.append((method, path, payload))
        if path.endswith("/pulls/12"):
            return 200, {"state": "open", "draft": False, "head": {"sha": self.head},
                         "base": {"ref": "main"}, "user": {"login": self.author},
                         "node_id": "PR_node"}
        if path.endswith("/commits/" + HEAD + "/check-runs?per_page=100"):
            runs = self.checks or [{"name": "Unit Tests", "head_sha": HEAD, "status": "completed",
                                    "conclusion": "success", "id": 1}]
            return 200, {"total_count": len(runs), "check_runs": runs}
        if path.endswith("/pulls/12/reviews?per_page=100"):
            return 200, self.reviews
        if path.endswith("/issues/7"):
            return 200, {"state": "open", "labels": [{"name": name} for name in self.labels]}
        if path.endswith("/pulls/12/reviews"):
            return 200, {"id": 44}
        if path == "/graphql":
            self.automerge = True
            return 200, {"data": {"enablePullRequestAutoMerge": {"pullRequest": {"number": 12}}}}
        if path.endswith("/issues/7/labels/agent-working") and method == "DELETE":
            self.labels.remove("agent-working")
            return 200, {}
        if path.endswith("/issues/7/labels") and method == "POST":
            self.labels.extend(payload["labels"])
            return 200, []
        raise AssertionError((method, path, payload))


class FakeWorkflow:
    def __init__(self, values=None):
        self.values = list(values or [snapshot()])
        self.transitions = []

    def observe(self, repo, issue):
        return self.values.pop(0) if len(self.values) > 1 else self.values[0]

    def transition_coordination_state(self, repo, issue, before, after, revision):
        self.transitions.append((repo, issue, before, after, revision))
        return True


class FakeReviewAgent:
    def __init__(self):
        self.launches = []
        self.observations = []

    def launch_review(self, repo, issue, pr, head):
        self.launches.append((repo, issue, pr, head))
        return {"execution_id": "review-session-1"}

    def observe_review(self, execution_id, repo, issue, pr, head):
        self.observations.append(execution_id)
        return {"status": "running"}

    def recover_review(self, repo, issue, pr, head):
        return "review-session-recovered"


class FakeHTTPResponse:
    status = 200
    def __init__(self, value): self.value = value
    def __enter__(self): return self
    def __exit__(self, *_): return None
    def read(self): return self.value


class LocalReviewerWriterTests(unittest.TestCase):
    def test_user_credential_provider_is_identity_checked_and_operation_fenced(self):
        token = "gho_" + "x" * 32
        calls = []

        def runner(command, **kwargs):
            calls.append(command)
            return type("RunnerResult", (), {"returncode": 0, "stdout": token + "\n"})()
        def opener(request, timeout):
            calls.append((request.get_method(), request.full_url, timeout))
            if request.full_url.endswith("/user"):
                return FakeHTTPResponse(b'{"login":"liuchangchxy"}')
            return FakeHTTPResponse(b'{"state":"open"}')

        provider = GitHubUserReviewerProvider(REPO, "liuchangchxy", runner=runner, opener=opener)
        self.assertEqual(provider.request("GET", f"/repos/{REPO}/pulls/12", REPO),
                         (200, {"state": "open"}))
        self.assertEqual(calls[0], ["gh", "auth", "token", "--hostname", "github.com"])
        with self.assertRaisesRegex(ValueError, "outside the configured Consumer"):
            provider.request("GET", "/repos/other/repo/pulls/12", "other/repo")
        with self.assertRaisesRegex(ValueError, "outside the Reviewer allowlist"):
            provider.request("DELETE", f"/repos/{REPO}/pulls/12", REPO)
        self.assertNotIn(token, repr(calls))

    def test_user_credential_provider_rejects_wrong_authenticated_identity(self):
        class RunnerResult:
            returncode = 0
            stdout = "not-shared"
        def opener(request, timeout): return FakeHTTPResponse(b'{"login":"another-user"}')
        provider = GitHubUserReviewerProvider(REPO, "liuchangchxy",
                                              runner=lambda *_a, **_k: RunnerResult(), opener=opener)
        with self.assertRaisesRegex(ValueError, "does not match configured Reviewer identity"):
            provider.request("GET", f"/repos/{REPO}/pulls/12", REPO)

    def test_approve_enables_native_auto_merge_before_exact_head_review(self):
        provider = FakeProvider()
        workflow = FakeWorkflow()
        writer = LocalReviewerWriter(provider, workflow, REPO, "liuchangchxy", MANIFEST)

        result = writer.submit(7, 12, HEAD, "APPROVE", "All Frozen Spec criteria pass.")

        self.assertEqual(result["status"], "review_submitted")
        writes = [call for call in provider.calls if call[0] == "POST"]
        self.assertEqual(writes[0][1], "/graphql")
        self.assertEqual(writes[1][1], f"/repos/{REPO}/pulls/12/reviews")
        self.assertTrue(provider.automerge)
        self.assertEqual(writes[1][2]["commit_id"], HEAD)

    def test_request_changes_submits_review_then_transitions_existing_coordination_state(self):
        provider = FakeProvider()
        workflow = FakeWorkflow()
        writer = LocalReviewerWriter(provider, workflow, REPO, "liuchangchxy", MANIFEST)

        result = writer.submit(7, 12, HEAD, "REQUEST_CHANGES", "The Frozen Spec criterion is not met.")

        self.assertEqual(result["status"], "review_submitted")
        self.assertEqual(workflow.transitions, [(REPO, 7, "agent-working", "changes-requested", "issue-revision")])
        self.assertNotIn("/graphql", [call[1] for call in provider.calls])

    def test_stale_head_fails_before_any_write(self):
        provider = FakeProvider(head="b" * 40)
        writer = LocalReviewerWriter(provider, FakeWorkflow(), REPO, "liuchangchxy", MANIFEST)

        with self.assertRaisesRegex(ValueError, "head SHA changed"):
            writer.submit(7, 12, HEAD, "APPROVE", "Approved.")

        self.assertFalse(any(call[0] != "GET" for call in provider.calls))

    def test_durable_reviewer_claim_is_unique_per_repository_pr_and_head(self):
        with tempfile.TemporaryDirectory() as root:
            store = _Store(str(Path(root) / "runtime.sqlite"))
            first, claimed = store.claim_reviewer_execution(REPO, 12, HEAD, "claude_code", 10)
            duplicate, claimed_again = store.claim_reviewer_execution(REPO.upper(), 12, HEAD.upper(),
                                                                        "antigravity", 11)
            next_head, claimed_next = store.claim_reviewer_execution(REPO, 12, "b" * 40,
                                                                       "claude_code", 12)

        self.assertTrue(claimed)
        self.assertFalse(claimed_again)
        self.assertTrue(claimed_next)
        self.assertEqual(first["provider"], "claude_code")
        self.assertEqual(duplicate["provider"], "claude_code")
        self.assertEqual(next_head["head_sha"], "b" * 40)

    def test_reviewer_execution_survives_store_restart(self):
        with tempfile.TemporaryDirectory() as root:
            database = str(Path(root) / "runtime.sqlite")
            first = _Store(database)
            first.claim_reviewer_execution(REPO, 12, HEAD, "claude_code", 10)
            first.update_reviewer_execution(REPO, 12, HEAD, "RUNNING", "session-1", now=11)
            restarted = _Store(database)
            observed = restarted.reviewer_execution(REPO, 12, HEAD)

        self.assertEqual(observed["state"], "RUNNING")
        self.assertEqual(observed["execution_id"], "session-1")

    def test_lifecycle_reviewer_driver_launches_once_and_observes_after_restart(self):
        with tempfile.TemporaryDirectory() as root:
            database = str(Path(root) / "runtime.sqlite")
            store = _Store(database)
            provider = FakeReviewAgent()
            workflow = FakeWorkflow()
            writer = LocalReviewerWriter(FakeProvider(), workflow, REPO, "liuchangchxy", MANIFEST)
            first = LocalReviewerDriver(store, provider, writer, "claude_code")

            started = first.advance(REPO, 7, 12, HEAD)
            restarted = LocalReviewerDriver(_Store(database), provider, writer, "claude_code")
            observed = restarted.advance(REPO, 7, 12, HEAD)

        self.assertEqual(started["status"], "reviewer_running")
        self.assertEqual(observed["status"], "reviewer_running")
        self.assertEqual(len(provider.launches), 1)
        self.assertEqual(provider.observations, ["review-session-1"])

    def test_restart_recovers_provider_session_after_launch_before_id_persistence(self):
        with tempfile.TemporaryDirectory() as root:
            store = _Store(str(Path(root) / "runtime.sqlite"))
            store.claim_reviewer_execution(REPO, 12, HEAD, "claude_code")
            provider = FakeReviewAgent()
            writer = LocalReviewerWriter(FakeProvider(), FakeWorkflow(), REPO,
                                         "liuchangchxy", MANIFEST)

            result = LocalReviewerDriver(store, provider, writer, "claude_code").advance(
                REPO, 7, 12, HEAD)
            record = store.reviewer_execution(REPO, 12, HEAD)

        self.assertEqual(result["status"], "reviewer_running")
        self.assertEqual(result["execution_id"], "review-session-recovered")
        self.assertEqual(record["state"], "RUNNING")
        self.assertEqual(record["execution_id"], "review-session-recovered")
        self.assertEqual(provider.launches, [])

    def test_implementer_identity_cannot_submit_its_own_review(self):
        provider = FakeProvider(author="liuchangchxy")
        writer = LocalReviewerWriter(provider, FakeWorkflow(), REPO, "liuchangchxy", MANIFEST)

        with self.assertRaisesRegex(ValueError, "reviewer identity matches PR author"):
            writer.submit(7, 12, HEAD, "APPROVE", "Approved.")

        self.assertFalse(any(call[0] != "GET" for call in provider.calls))

    def test_failed_required_check_blocks_both_review_decisions(self):
        provider = FakeProvider(checks=[{"name": "Unit Tests", "head_sha": HEAD,
                                        "status": "completed", "conclusion": "failure", "id": 2}])
        writer = LocalReviewerWriter(provider, FakeWorkflow([snapshot(check_runs=tuple(provider.checks))]), REPO, "liuchangchxy", MANIFEST)

        with self.assertRaisesRegex(ValueError, "required checks are not accepted"):
            writer.submit(7, 12, HEAD, "REQUEST_CHANGES", "This must not be a product rejection.")

        self.assertFalse(any(call[0] != "GET" for call in provider.calls))


if __name__ == "__main__":
    unittest.main()
