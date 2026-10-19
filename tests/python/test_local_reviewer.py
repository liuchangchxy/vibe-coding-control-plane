import unittest
import tempfile
import json
from pathlib import Path

from vccp_runtime.core import WorkflowSnapshot, _Store
from vccp_runtime.local_reviewer import (GitHubUserReviewerProvider, LocalReviewerDriver,
                                         LocalReviewerWriter)
from vccp_runtime.reviewer_agents import ClaudeAgentSDKReviewer, ReviewerTaskError


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

    def __init__(self, head=HEAD, author="builder[bot]", checks=None, reviews=None,
                 pr_state="open"):
        self.head, self.author = head, author
        self.pr_state = pr_state
        self.checks = checks
        self.reviews = reviews or []
        self.calls = []
        self.labels = ["frozen-spec", "agent-working"]
        self.automerge = False

    def request(self, method, path, repo, payload=None):
        self.calls.append((method, path, payload))
        if path.endswith("/pulls/12"):
            return 200, {"state": self.pr_state, "draft": False, "head": {"sha": self.head},
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

    def verify_identity(self):
        return {"login": "liuchangchxy", "verified": True}


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
    def __init__(self, retry_evidence=None, execution=None):
        self.launches = []
        self.observations = []
        self.retry_evidence = retry_evidence or {"status": "none"}
        self.execution = execution or {"execution_id": "review-session-1"}
        self.retry_arguments = []

    def launch_review(self, repo, issue, pr, head):
        self.launches.append((repo, issue, pr, head))
        return dict(self.execution)

    def observe_review(self, execution_id, repo, issue, pr, head):
        self.observations.append(execution_id)
        return {"status": "running"}

    def recover_review(self, repo, issue, pr, head):
        return "review-session-recovered"

    def retry_session_evidence(self, repo, issue, pr, head, **kwargs):
        self.retry_arguments.append(kwargs)
        return self.retry_evidence


class FailedReviewAgent(FakeReviewAgent):
    def launch_review(self, repo, issue, pr, head):
        raise ReviewerTaskError("issue", f"/repos/{repo}/issues/{issue}", 503, "URLError")


class FakeHTTPResponse:
    status = 200
    def __init__(self, value): self.value = value
    def __enter__(self): return self
    def __exit__(self, *_): return None
    def read(self): return self.value


class LocalReviewerWriterTests(unittest.TestCase):
    def test_duplicate_sdk_checkpoints_are_deduplicated_before_sqlite_write(self):
        with tempfile.TemporaryDirectory() as root:
            store = _Store(str(Path(root) / "runtime.sqlite"))
            store.claim_reviewer_execution(REPO, 12, HEAD, "claude_agent_sdk", now=100)
            driver = LocalReviewerDriver(
                store, FakeReviewAgent(),
                LocalReviewerWriter(FakeProvider(), FakeWorkflow(), REPO,
                                    "liuchangchxy", MANIFEST), "claude_agent_sdk")
            original_update = store.update_reviewer_execution
            writes = []

            def fail_if_duplicate(*args, **kwargs):
                outcome = kwargs.get("outcome")
                if outcome is None and len(args) > 5:
                    outcome = args[5]
                if outcome and outcome.startswith("sdk_review_checkpoint:"):
                    if outcome in writes:
                        raise AssertionError("duplicate checkpoint reached SQLite")
                    writes.append(outcome)
                return original_update(*args, **kwargs)

            store.update_reviewer_execution = fail_if_duplicate
            checkpoint = {"phase": "active", "session_id": "sdk-session",
                          "resume_count": 0, "total_turns": 0}

            driver._record_sdk_checkpoint(REPO, 12, HEAD, checkpoint)
            driver._record_sdk_checkpoint(REPO, 12, HEAD, checkpoint)
            record = store.reviewer_execution(REPO, 12, HEAD)

        self.assertEqual(len(writes), 1)
        self.assertTrue(record["outcome"].startswith("sdk_review_checkpoint:"))

    def test_critical_sdk_checkpoint_write_failure_fails_closed(self):
        with tempfile.TemporaryDirectory() as root:
            store = _Store(str(Path(root) / "runtime.sqlite"))
            store.claim_reviewer_execution(REPO, 12, HEAD, "claude_agent_sdk", now=100)
            driver = LocalReviewerDriver(
                store, FakeReviewAgent(),
                LocalReviewerWriter(FakeProvider(), FakeWorkflow(), REPO,
                                    "liuchangchxy", MANIFEST), "claude_agent_sdk")
            store.update_reviewer_execution = lambda *args, **kwargs: False

            with self.assertRaisesRegex(RuntimeError, "checkpoint could not be persisted"):
                driver._record_sdk_checkpoint(
                    REPO, 12, HEAD, {"phase": "active", "session_id": "sdk-session",
                                     "resume_count": 0, "total_turns": 0})

    def test_checkpoint_session_mismatch_transitions_running_execution_to_failed(self):
        class ResumableAgent(FakeReviewAgent):
            def resume_review(self, *args, **kwargs):
                raise AssertionError("mismatched session must not be resumed")

        with tempfile.TemporaryDirectory() as root:
            store = _Store(str(Path(root) / "runtime.sqlite"))
            store.claim_reviewer_execution(REPO, 12, HEAD, "claude_agent_sdk", now=100)
            checkpoint = {"phase": "resume_ready", "session_id": "checkpoint-session",
                          "resume_count": 1, "total_turns": 31}
            store.update_reviewer_execution(
                REPO, 12, HEAD, "RUNNING", "row-session",
                "sdk_review_checkpoint:" + json.dumps(checkpoint, separators=(",", ":")), now=110)
            driver = LocalReviewerDriver(
                store, ResumableAgent(),
                LocalReviewerWriter(FakeProvider(), FakeWorkflow(), REPO,
                                    "liuchangchxy", MANIFEST), "claude_agent_sdk")

            result = driver.advance(REPO, 7, 12, HEAD)
            record = store.reviewer_execution(REPO, 12, HEAD)

        self.assertEqual(result["reason"], "checkpoint_session_mismatch")
        self.assertEqual(record["state"], "FAILED")
        self.assertEqual(record["outcome"], "sdk_terminal_failure:checkpoint_session_mismatch")

    def test_invalid_completed_decisions_transition_to_failed(self):
        class ResumableAgent(FakeReviewAgent):
            def resume_review(self, *args, **kwargs):
                raise AssertionError("invalid completed checkpoint must not resume")

        scenarios = (
            ("RUNNING", "sdk_review_checkpoint:" + json.dumps({
                "phase": "completed", "session_id": "sdk-session", "resume_count": 1,
                "total_turns": 42, "result": {"event": "BLOCKED", "body": "incomplete"}},
                separators=(",", ":")), "reviewer_result_unavailable", "completed_checkpoint_invalid"),
            ("COMPLETED", json.dumps({"event": "BLOCKED", "body": "incomplete"}),
             "reviewer_invalid_decision", "invalid_reviewer_decision"),
        )
        for initial_state, outcome, expected_status, expected_outcome in scenarios:
            with self.subTest(initial_state=initial_state), tempfile.TemporaryDirectory() as root:
                store = _Store(str(Path(root) / "runtime.sqlite"))
                store.claim_reviewer_execution(REPO, 12, HEAD, "claude_agent_sdk", now=100)
                store.update_reviewer_execution(REPO, 12, HEAD, initial_state, "sdk-session",
                                                outcome=outcome, now=110)
                driver = LocalReviewerDriver(
                    store, ResumableAgent(),
                    LocalReviewerWriter(FakeProvider(), FakeWorkflow(), REPO,
                                        "liuchangchxy", MANIFEST), "claude_agent_sdk")

                result = driver.advance(REPO, 7, 12, HEAD)
                record = store.reviewer_execution(REPO, 12, HEAD)

                self.assertEqual(result["status"], expected_status)
                self.assertEqual(record["state"], "FAILED")
                self.assertEqual(record["outcome"], expected_outcome)

    def test_runner_restart_resumes_only_durable_turn_boundary_checkpoint(self):
        class IdempotentProvider(FakeProvider):
            def request(self, method, path, repo, payload=None):
                response = super().request(method, path, repo, payload)
                if method == "POST" and path.endswith("/pulls/12/reviews"):
                    self.reviews.append({"state": "APPROVED", "commit_id": HEAD,
                                         "user": {"login": "liuchangchxy"}})
                return response

        class ResumableAgent(FakeReviewAgent):
            def __init__(self):
                super().__init__()
                self.resumes = []

            def resume_review(self, repo, issue, pr, head, session_id, resume_count,
                              total_turns, started_at, *, on_checkpoint=None):
                self.resumes.append((session_id, resume_count, total_turns))
                decision = {"event": "APPROVE", "body": "Completed after resume."}
                on_checkpoint({"phase": "completed", "session_id": session_id,
                               "resume_count": resume_count, "total_turns": total_turns,
                               "result": decision})
                return {"status": "completed", "execution_id": session_id, **decision}

        with tempfile.TemporaryDirectory() as root:
            database = str(Path(root) / "runtime.sqlite")
            store = _Store(database)
            store.claim_reviewer_execution(REPO, 12, HEAD, "claude_agent_sdk", now=100)
            checkpoint = {"phase": "resume_ready", "session_id": "sdk-session",
                          "resume_count": 1, "total_turns": 30}
            store.update_reviewer_execution(
                REPO, 12, HEAD, "RUNNING", "sdk-session",
                "sdk_review_checkpoint:" + json.dumps(checkpoint, separators=(",", ":")), now=110)
            agent, provider = ResumableAgent(), IdempotentProvider()
            writer = LocalReviewerWriter(provider, FakeWorkflow(), REPO, "liuchangchxy", MANIFEST)
            restarted = LocalReviewerDriver(_Store(database), agent, writer, "claude_agent_sdk")

            first = restarted.advance(REPO, 7, 12, HEAD)
            second = restarted.advance(REPO, 7, 12, HEAD)
            record = _Store(database).reviewer_execution(REPO, 12, HEAD)

        self.assertEqual(first["status"], "review_decision_submitted")
        self.assertEqual(second["status"], "formal_review_exists")
        self.assertEqual(agent.resumes, [("sdk-session", 1, 30)])
        self.assertEqual(sum(call[0] == "POST" and call[1].endswith("/pulls/12/reviews")
                             for call in provider.calls), 1)
        self.assertEqual(record["state"], "DECIDED")

    def test_runner_restart_fails_closed_for_active_unknown_sdk_checkpoint(self):
        class ResumableAgent(FakeReviewAgent):
            def __init__(self): super().__init__(); self.resumes = []
            def resume_review(self, *args, **kwargs): self.resumes.append(args); raise AssertionError("must not resume")

        with tempfile.TemporaryDirectory() as root:
            store = _Store(str(Path(root) / "runtime.sqlite"))
            store.claim_reviewer_execution(REPO, 12, HEAD, "claude_agent_sdk", now=100)
            checkpoint = {"phase": "active", "session_id": "sdk-session",
                          "resume_count": 0, "total_turns": 0}
            store.update_reviewer_execution(
                REPO, 12, HEAD, "RUNNING", "sdk-session",
                "sdk_review_checkpoint:" + json.dumps(checkpoint, separators=(",", ":")), now=110)
            agent = ResumableAgent()
            writer = LocalReviewerWriter(FakeProvider(), FakeWorkflow(), REPO, "liuchangchxy", MANIFEST)

            result = LocalReviewerDriver(store, agent, writer, "claude_agent_sdk").advance(
                REPO, 7, 12, HEAD)
            record = store.reviewer_execution(REPO, 12, HEAD)

        self.assertEqual(result["status"], "reviewer_execution_failed")
        self.assertEqual(agent.resumes, [])
        self.assertEqual(record["state"], "FAILED")

    def test_runner_restart_rejects_resume_when_head_changed_or_formal_review_exists(self):
        class ResumableAgent(FakeReviewAgent):
            def __init__(self): super().__init__(); self.resumes = []
            def resume_review(self, *args, **kwargs): self.resumes.append(args); raise AssertionError("must not resume")

        scenarios = (
            (FakeProvider(head="b" * 40), "reviewer_preconditions_changed", "FAILED"),
            (FakeProvider(reviews=[{"state": "COMMENTED", "commit_id": HEAD,
                                    "user": {"login": "someone-else"}}]),
             "formal_review_exists", "DECIDED"),
        )
        for provider, expected_status, expected_state in scenarios:
            with self.subTest(expected_status=expected_status), tempfile.TemporaryDirectory() as root:
                store = _Store(str(Path(root) / "runtime.sqlite"))
                store.claim_reviewer_execution(REPO, 12, HEAD, "claude_agent_sdk", now=100)
                checkpoint = {"phase": "resume_ready", "session_id": "sdk-session",
                              "resume_count": 1, "total_turns": 30}
                store.update_reviewer_execution(
                    REPO, 12, HEAD, "RUNNING", "sdk-session",
                    "sdk_review_checkpoint:" + json.dumps(checkpoint, separators=(",", ":")), now=110)
                agent = ResumableAgent()
                writer = LocalReviewerWriter(provider, FakeWorkflow(), REPO, "liuchangchxy", MANIFEST)

                result = LocalReviewerDriver(store, agent, writer, "claude_agent_sdk").advance(
                    REPO, 7, 12, HEAD)
                record = store.reviewer_execution(REPO, 12, HEAD)

                self.assertEqual(result["status"], expected_status)
                self.assertEqual(agent.resumes, [])
                self.assertEqual(record["state"], expected_state)

    def test_runner_restart_replays_completed_checkpoint_without_sdk_call(self):
        class Agent(FakeReviewAgent):
            def __init__(self): super().__init__(); self.resumes = []
            def resume_review(self, *args, **kwargs): self.resumes.append(args); raise AssertionError("must not resume")

        with tempfile.TemporaryDirectory() as root:
            store = _Store(str(Path(root) / "runtime.sqlite"))
            store.claim_reviewer_execution(REPO, 12, HEAD, "claude_agent_sdk", now=100)
            result_value = {"event": "REQUEST_CHANGES", "body": "A blocking issue."}
            checkpoint = {"phase": "completed", "session_id": "sdk-session",
                          "resume_count": 1, "total_turns": 42, "result": result_value}
            store.update_reviewer_execution(
                REPO, 12, HEAD, "RUNNING", "sdk-session",
                "sdk_review_checkpoint:" + json.dumps(checkpoint, separators=(",", ":")), now=110)
            agent, provider = Agent(), FakeProvider()
            writer = LocalReviewerWriter(provider, FakeWorkflow(), REPO, "liuchangchxy", MANIFEST)

            result = LocalReviewerDriver(store, agent, writer, "claude_agent_sdk").advance(
                REPO, 7, 12, HEAD)

        self.assertEqual(result["status"], "review_decision_submitted")
        self.assertEqual(result["event"], "REQUEST_CHANGES")
        self.assertEqual(agent.resumes, [])

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

    def test_formal_review_appearing_during_revalidation_prevents_duplicate_write(self):
        class RacingProvider(FakeProvider):
            def __init__(self):
                super().__init__()
                self.review_reads = 0

            def request(self, method, path, repo, payload=None):
                if path.endswith("/pulls/12/reviews?per_page=100"):
                    self.review_reads += 1
                    if self.review_reads == 2:
                        self.reviews = [{"commit_id": HEAD, "state": "APPROVED"}]
                return super().request(method, path, repo, payload)

        provider = RacingProvider()
        writer = LocalReviewerWriter(provider, FakeWorkflow(), REPO, "liuchangchxy", MANIFEST)

        result = writer.submit(7, 12, HEAD, "APPROVE", "Approved.")

        self.assertEqual(result["status"], "formal_review_exists")
        self.assertFalse(any(call[0] == "POST" and call[1].endswith("/pulls/12/reviews")
                             for call in provider.calls))

    def test_durable_reviewer_claim_is_unique_per_repository_pr_and_head(self):
        with tempfile.TemporaryDirectory() as root:
            store = _Store(str(Path(root) / "runtime.sqlite"))
            first, claimed = store.claim_reviewer_execution(REPO, 12, HEAD, "claude_agent_sdk", 10)
            duplicate, claimed_again = store.claim_reviewer_execution(REPO.upper(), 12, HEAD.upper(),
                                                                        "antigravity", 11)
            next_head, claimed_next = store.claim_reviewer_execution(REPO, 12, "b" * 40,
                                                                       "claude_agent_sdk", 12)

        self.assertTrue(claimed)
        self.assertFalse(claimed_again)
        self.assertTrue(claimed_next)
        self.assertEqual(first["provider"], "claude_agent_sdk")
        self.assertEqual(duplicate["provider"], "claude_agent_sdk")
        self.assertEqual(next_head["head_sha"], "b" * 40)

    def test_reviewer_execution_survives_store_restart(self):
        with tempfile.TemporaryDirectory() as root:
            database = str(Path(root) / "runtime.sqlite")
            first = _Store(database)
            first.claim_reviewer_execution(REPO, 12, HEAD, "claude_agent_sdk", 10)
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
            first = LocalReviewerDriver(store, provider, writer, "claude_agent_sdk")

            started = first.advance(REPO, 7, 12, HEAD)
            restarted = LocalReviewerDriver(_Store(database), provider, writer, "claude_agent_sdk")
            observed = restarted.advance(REPO, 7, 12, HEAD)

        self.assertEqual(started["status"], "reviewer_running")
        self.assertEqual(observed["status"], "reviewer_running")
        self.assertEqual(len(provider.launches), 1)
        self.assertEqual(provider.observations, ["review-session-1"])

    def test_sdk_completion_persists_real_session_before_formal_review_write(self):
        with tempfile.TemporaryDirectory() as root:
            store = _Store(str(Path(root) / "runtime.sqlite"))
            provider = FakeProvider()
            states_at_formal_write = []
            def record_state(method, path, _payload):
                if method == "POST" and path.endswith("/pulls/12/reviews"):
                    states_at_formal_write.append(store.reviewer_execution(REPO, 12, HEAD))
            original_request = provider.request
            def observed_request(method, path, repo, payload=None):
                record_state(method, path, payload)
                return original_request(method, path, repo, payload)
            provider.request = observed_request
            agent = FakeReviewAgent(execution={
                "status": "completed", "execution_id": "sdk-session-id",
                "event": "APPROVE", "body": "All criteria pass."})
            writer = LocalReviewerWriter(provider, FakeWorkflow(), REPO, "liuchangchxy", MANIFEST)

            result = LocalReviewerDriver(store, agent, writer, "claude_agent_sdk").advance(
                REPO, 7, 12, HEAD)
            record = store.reviewer_execution(REPO, 12, HEAD)

        self.assertEqual(result["status"], "review_decision_submitted")
        self.assertEqual(result["event"], "APPROVE")
        self.assertEqual(states_at_formal_write[0]["state"], "COMPLETED")
        self.assertEqual(states_at_formal_write[0]["execution_id"], "sdk-session-id")
        self.assertEqual(record["state"], "DECIDED")
        self.assertEqual(record["provider"], "claude_agent_sdk")

    def test_sdk_completed_result_replays_after_store_restart_without_second_query(self):
        class InterruptedWriteProvider(FakeProvider):
            def request(self, method, path, repo, payload=None):
                if path == "/graphql": raise RuntimeError("temporary GitHub write interruption")
                return super().request(method, path, repo, payload)

        with tempfile.TemporaryDirectory() as root:
            database = str(Path(root) / "runtime.sqlite")
            first_store = _Store(database)
            first_agent = FakeReviewAgent(execution={
                "status": "completed", "execution_id": "sdk-session-id",
                "event": "APPROVE", "body": "All criteria pass."})
            interrupted_writer = LocalReviewerWriter(InterruptedWriteProvider(), FakeWorkflow(),
                                                    REPO, "liuchangchxy", MANIFEST)
            first = LocalReviewerDriver(first_store, first_agent, interrupted_writer, "claude_agent_sdk")
            with self.assertRaisesRegex(RuntimeError, "temporary GitHub write interruption"):
                first.advance(REPO, 7, 12, HEAD)
            completed = first_store.reviewer_execution(REPO, 12, HEAD)

            restarted_agent = FakeReviewAgent()
            writer = LocalReviewerWriter(FakeProvider(), FakeWorkflow(), REPO, "liuchangchxy", MANIFEST)
            result = LocalReviewerDriver(_Store(database), restarted_agent, writer,
                                         "claude_agent_sdk").advance(REPO, 7, 12, HEAD)
            decided = _Store(database).reviewer_execution(REPO, 12, HEAD)

        self.assertEqual(completed["state"], "COMPLETED")
        self.assertEqual(completed["execution_id"], "sdk-session-id")
        self.assertEqual(result["status"], "review_decision_submitted")
        self.assertEqual(restarted_agent.launches, [])
        self.assertEqual(decided["state"], "DECIDED")

    def test_historical_claude_code_execution_is_not_claimed_by_sdk_provider(self):
        with tempfile.TemporaryDirectory() as root:
            store = _Store(str(Path(root) / "runtime.sqlite"))
            store.claim_reviewer_execution(REPO, 12, HEAD, "claude_code", 10)
            store.update_reviewer_execution(REPO, 12, HEAD, "RUNNING", "legacy-cli-session", now=11)
            agent = FakeReviewAgent()
            writer = LocalReviewerWriter(FakeProvider(), FakeWorkflow(), REPO,
                                         "liuchangchxy", MANIFEST)

            result = LocalReviewerDriver(store, agent, writer, "claude_agent_sdk").advance(
                REPO, 7, 12, HEAD)
            record = store.reviewer_execution(REPO, 12, HEAD)

        self.assertEqual(result["status"], "reviewer_provider_mismatch")
        self.assertEqual(result["record_provider"], "claude_code")
        self.assertEqual(record["execution_id"], "legacy-cli-session")
        self.assertEqual(record["state"], "RUNNING")
        self.assertEqual(agent.launches, [])

    def test_restart_recovers_provider_session_after_launch_before_id_persistence(self):
        with tempfile.TemporaryDirectory() as root:
            store = _Store(str(Path(root) / "runtime.sqlite"))
            store.claim_reviewer_execution(REPO, 12, HEAD, "claude_agent_sdk")
            provider = FakeReviewAgent()
            writer = LocalReviewerWriter(FakeProvider(), FakeWorkflow(), REPO,
                                         "liuchangchxy", MANIFEST)

            result = LocalReviewerDriver(store, provider, writer, "claude_agent_sdk").advance(
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

    def test_exact_head_manual_retry_preserves_failed_evidence_and_launches_once(self):
        with tempfile.TemporaryDirectory() as root:
            store = _Store(str(Path(root) / "runtime.sqlite"))
            store.claim_reviewer_execution(REPO, 12, HEAD, "claude_agent_sdk", 10)
            store.update_reviewer_execution(REPO, 12, HEAD, "FAILED",
                                            outcome="sdk_not_started:stage=issue", now=11)
            agent = FakeReviewAgent()
            writer = LocalReviewerWriter(FakeProvider(), FakeWorkflow(), REPO,
                                         "liuchangchxy", MANIFEST)
            driver = LocalReviewerDriver(store, agent, writer, "claude_agent_sdk")

            result = driver.retry_failed(REPO, 7, 12, HEAD, authorization_head=HEAD)
            record = store.reviewer_execution(REPO, 12, HEAD)
            history = store.reviewer_retry_history(REPO, 12, HEAD)

        self.assertEqual(result["status"], "reviewer_running")
        self.assertEqual(result["head_sha"], HEAD)
        self.assertEqual(agent.launches, [(REPO, 7, 12, HEAD)])
        self.assertEqual(record["state"], "RUNNING")
        self.assertEqual(record["execution_id"], "review-session-1")
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["previous_outcome"], "sdk_not_started:stage=issue")
        self.assertEqual(history[0]["authorized_by"], "liuchangchxy")

    def test_terminal_failed_sdk_session_can_retry_with_prior_execution_preserved(self):
        with tempfile.TemporaryDirectory() as root:
            store = _Store(str(Path(root) / "runtime.sqlite"))
            store.claim_reviewer_execution(REPO, 12, HEAD, "claude_agent_sdk", 10)
            store.update_reviewer_execution(REPO, 12, HEAD, "RUNNING", "failed-session-id", now=11)
            store.update_reviewer_execution(REPO, 12, HEAD, "FAILED",
                                            outcome="sdk_terminal_failure:unsupported_result", now=12)
            agent = FakeReviewAgent({"status": "terminal_failed", "execution_id": "failed-session-id",
                                     "state": "failed"})
            writer = LocalReviewerWriter(FakeProvider(), FakeWorkflow(), REPO,
                                         "liuchangchxy", MANIFEST)

            result = LocalReviewerDriver(store, agent, writer, "claude_agent_sdk").retry_failed(
                REPO, 7, 12, HEAD, authorization_head=HEAD)

            record = store.reviewer_execution(REPO, 12, HEAD)
            history = store.reviewer_retry_history(REPO, 12, HEAD)

        self.assertEqual(result["status"], "reviewer_running")
        self.assertEqual(record["execution_id"], "review-session-1")
        self.assertEqual(history[-1]["previous_execution_id"], "failed-session-id")
        self.assertEqual(history[-1]["previous_outcome"], "sdk_terminal_failure:unsupported_result")

    def test_persisted_result_error_execution_can_retry_without_state_edit(self):
        class ResultErrorAgent(FakeReviewAgent):
            retry_session_evidence = ClaudeAgentSDKReviewer.retry_session_evidence

        with tempfile.TemporaryDirectory() as root:
            store = _Store(str(Path(root) / "runtime.sqlite"))
            store.claim_reviewer_execution(REPO, 12, HEAD, "claude_agent_sdk", 10)
            store.update_reviewer_execution(REPO, 12, HEAD, "RUNNING", "failed-session-id", now=11)
            store.update_reviewer_execution(
                REPO, 12, HEAD, "FAILED", outcome=(
                    "sdk_execution_state_unknown:stage=claude_agent_sdk_execution "
                    "endpoint=none http_status=none exception_type=ResultError process_status=none"), now=12)
            agent = ResultErrorAgent()
            writer = LocalReviewerWriter(FakeProvider(), FakeWorkflow(), REPO,
                                         "liuchangchxy", MANIFEST)

            result = LocalReviewerDriver(store, agent, writer, "claude_agent_sdk").retry_failed(
                REPO, 7, 12, HEAD, authorization_head=HEAD)

            record = store.reviewer_execution(REPO, 12, HEAD)
            history = store.reviewer_retry_history(REPO, 12, HEAD)

        self.assertEqual(result["status"], "reviewer_running")
        self.assertEqual(agent.launches, [(REPO, 7, 12, HEAD)])
        self.assertEqual(record["state"], "RUNNING")
        self.assertEqual(record["execution_id"], "review-session-1")
        self.assertEqual(history[-1]["previous_execution_id"], "failed-session-id")
        self.assertIn("exception_type=ResultError", history[-1]["previous_outcome"])

    def test_retry_requires_explicit_matching_authorization_sha(self):
        with tempfile.TemporaryDirectory() as root:
            store = _Store(str(Path(root) / "runtime.sqlite"))
            store.claim_reviewer_execution(REPO, 12, HEAD, "claude_agent_sdk", 10)
            store.update_reviewer_execution(REPO, 12, HEAD, "FAILED", outcome="launch failed", now=11)
            agent = FakeReviewAgent()
            writer = LocalReviewerWriter(FakeProvider(), FakeWorkflow(), REPO,
                                         "liuchangchxy", MANIFEST)

            result = LocalReviewerDriver(store, agent, writer, "claude_agent_sdk").retry_failed(
                REPO, 7, 12, HEAD, authorization_head="b" * 40)

            record = store.reviewer_execution(REPO, 12, HEAD)
            history = store.reviewer_retry_history(REPO, 12, HEAD)

        self.assertEqual(result["status"], "reviewer_retry_rejected")
        self.assertEqual(record["state"], "FAILED")
        self.assertEqual(agent.launches, [])
        self.assertEqual(history, [])

    def test_retry_rejects_execution_id_or_unknown_sdk_session_evidence(self):
        for execution_id, evidence in (("session-already-known", {"status": "none"}),
                                       ("session-already-known", {"status": "terminal_failed",
                                                                   "execution_id": "different-session",
                                                                   "state": "failed"}),
                                       (None, {"status": "present", "count": 1})):
            with self.subTest(execution_id=execution_id, evidence=evidence), \
                    tempfile.TemporaryDirectory() as root:
                store = _Store(str(Path(root) / "runtime.sqlite"))
                store.claim_reviewer_execution(REPO, 12, HEAD, "claude_agent_sdk", 10)
                store.update_reviewer_execution(REPO, 12, HEAD, "FAILED", execution_id,
                                                "launch failed", now=11)
                agent = FakeReviewAgent(evidence)
                writer = LocalReviewerWriter(FakeProvider(), FakeWorkflow(), REPO,
                                             "liuchangchxy", MANIFEST)

                result = LocalReviewerDriver(store, agent, writer, "claude_agent_sdk").retry_failed(
                    REPO, 7, 12, HEAD, authorization_head=HEAD)

                self.assertEqual(result["status"], "reviewer_retry_rejected")
                self.assertEqual(agent.launches, [])
                self.assertEqual(store.reviewer_retry_history(REPO, 12, HEAD), [])

    def test_retry_rejects_running_session_even_when_prior_execution_id_matches(self):
        with tempfile.TemporaryDirectory() as root:
            store = _Store(str(Path(root) / "runtime.sqlite"))
            store.claim_reviewer_execution(REPO, 12, HEAD, "claude_agent_sdk", 10)
            store.update_reviewer_execution(REPO, 12, HEAD, "RUNNING", "live-session-id", now=11)
            store.update_reviewer_execution(REPO, 12, HEAD, "FAILED", outcome="observation failed", now=12)
            agent = FakeReviewAgent({"status": "present", "count": 1})
            writer = LocalReviewerWriter(FakeProvider(), FakeWorkflow(), REPO,
                                         "liuchangchxy", MANIFEST)

            result = LocalReviewerDriver(store, agent, writer, "claude_agent_sdk").retry_failed(
                REPO, 7, 12, HEAD, authorization_head=HEAD)

        self.assertEqual(result["status"], "reviewer_retry_rejected")
        self.assertEqual(agent.launches, [])

    def test_retry_fails_closed_on_changed_or_closed_pr_and_existing_formal_review(self):
        scenarios = (
            (FakeProvider(head="b" * 40), []),
            (FakeProvider(pr_state="closed"), []),
            (FakeProvider(reviews=[{"state": "APPROVED", "commit_id": HEAD,
                                   "user": {"login": "someone-else"}}]), []),
        )
        for provider, _ in scenarios:
            with self.subTest(provider=provider), tempfile.TemporaryDirectory() as root:
                store = _Store(str(Path(root) / "runtime.sqlite"))
                store.claim_reviewer_execution(REPO, 12, HEAD, "claude_agent_sdk", 10)
                store.update_reviewer_execution(REPO, 12, HEAD, "FAILED", outcome="launch failed", now=11)
                agent = FakeReviewAgent()
                writer = LocalReviewerWriter(provider, FakeWorkflow(), REPO,
                                             "liuchangchxy", MANIFEST)

                result = LocalReviewerDriver(store, agent, writer, "claude_agent_sdk").retry_failed(
                    REPO, 7, 12, HEAD, authorization_head=HEAD)

                self.assertEqual(result["status"], "reviewer_retry_rejected")
                self.assertEqual(agent.launches, [])
                self.assertEqual(store.reviewer_retry_history(REPO, 12, HEAD), [])

    def test_retry_fails_closed_when_sdk_execution_state_is_unknown(self):
        with tempfile.TemporaryDirectory() as root:
            store = _Store(str(Path(root) / "runtime.sqlite"))
            store.claim_reviewer_execution(REPO, 12, HEAD, "claude_agent_sdk", 10)
            store.update_reviewer_execution(REPO, 12, HEAD, "FAILED", "possibly-running",
                                            outcome="sdk_execution_state_unknown:transport", now=11)
            agent = FakeReviewAgent({"status": "unobservable", "exception_type": "ExecutionStateUnknown"})
            writer = LocalReviewerWriter(FakeProvider(), FakeWorkflow(), REPO,
                                         "liuchangchxy", MANIFEST)

            result = LocalReviewerDriver(store, agent, writer, "claude_agent_sdk").retry_failed(
                REPO, 7, 12, HEAD, authorization_head=HEAD)

        self.assertEqual(result["status"], "reviewer_retry_rejected")
        self.assertEqual(agent.launches, [])

    def test_reviewer_launch_failure_persists_safe_stage_diagnostics(self):
        with tempfile.TemporaryDirectory() as root:
            store = _Store(str(Path(root) / "runtime.sqlite"))
            agent = FailedReviewAgent()
            writer = LocalReviewerWriter(FakeProvider(), FakeWorkflow(), REPO,
                                         "liuchangchxy", MANIFEST)

            result = LocalReviewerDriver(store, agent, writer, "claude_agent_sdk").advance(REPO, 7, 12, HEAD)
            record = store.reviewer_execution(REPO, 12, HEAD)

        self.assertEqual(result["status"], "reviewer_launch_failed")
        self.assertIn("stage=issue", result["detail"])
        self.assertIn(f"endpoint=/repos/{REPO}/issues/7", result["detail"])
        self.assertIn("http_status=503", result["detail"])
        self.assertIn("exception_type=URLError", result["detail"])
        self.assertEqual(record["state"], "FAILED")
        self.assertIn("stage=issue", record["outcome"])


if __name__ == "__main__":
    unittest.main()
