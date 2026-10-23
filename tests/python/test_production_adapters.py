import json
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock
from pathlib import Path
from types import SimpleNamespace

from vccp_runtime.adapters import (AntiGravityImplementer, GitHubAPI, GitHubAppWriter, GitHubWorkflowAdapter,
                                   WorkspaceWriteGuard, build_runtime, generic_prompt)
from vccp_runtime.core import LaunchDisposition, LaunchRequest, RepairCandidate, RuntimeCore
from vccp_runtime.lifecycle import LifecycleDriver
from vccp_runtime import runner as runtime_runner
from vccp_runtime import controlled_cli


SHA = "a" * 40
CONVERSATION_ID = "e13f972a-83b8-4f8a-9a6f-8c7d3b2a1f05"
MANIFEST = {"schema_version": 2, "issue_contract": {"max_automated_repairs": 3,
    "frozen_spec_label": "frozen-spec", "active_coordination_labels": ["agent-ready", "agent-working", "changes-requested"],
    "terminal_coordination_labels": ["infra-blocked", "needs-human"]},
    "repository": {"base_branch": "main", "implementer_authors": ["agent"]},
    "reviewer": {"required_checks": [{"name": "Unit Tests", "accepted_conclusions": ["success"]}]}}


class FakeAPI:
    def __init__(self):
        self.issue = {"state": "open", "updated_at": "r1", "labels": [
            {"name": "agent-ready"}, {"name": "frozen-spec"}]}
        self.canonical_events = []
        self.has_next_page = False
        self.pr = None
        self.merged_pr = None
        self.reviews = []
        self.check_runs = []
        self.reads = []
        self.discovery = {"agent-ready": [], "agent-working": [], "changes-requested": []}

    def get(self, path):
        self.reads.append(path)
        if "/issues?" in path:
            if "agent-ready" in path: return self.discovery["agent-ready"]
            if "agent-working" in path: return self.discovery["agent-working"]
            if "changes-requested" in path: return self.discovery["changes-requested"]
            return []
        if "/reviews?per_page=100&page=" in path:
            page = int(path.rsplit("page=", 1)[1])
            return self.reviews[(page - 1) * 100:page * 100]
        if "/check-runs?per_page=100" in path:
            return {"total_count": len(self.check_runs), "check_runs": self.check_runs}
        if "/pulls/" in path: return self.pr
        return self.issue

    def graphql(self, query, variables):
        self.query = query
        self.variables = variables
        if "pullRequest(number:" in query:
            return {"repository": {"pullRequest": self.merged_pr}}
        return {"repository": {"issue": {"timelineItems": {
            "nodes": self.canonical_events, "pageInfo": {"hasNextPage": self.has_next_page}}}}}


class FakeWriter:
    def __init__(self): self.calls = []; self.success = True
    def replace_labels(self, *args): self.calls.append(args); return self.success


class MutatingWriter(FakeWriter):
    def __init__(self, api): super().__init__(); self.api = api
    def replace_labels(self, repo, issue, remove, add):
        super().replace_labels(repo, issue, remove, add)
        if self.success:
            self.api.issue["labels"] = [x for x in self.api.issue["labels"] if x["name"] not in remove]
            self.api.issue["labels"].extend({"name": x} for x in add)
            self.api.issue["updated_at"] += "+"
        return self.success


class FakeProductionCommands:
    def __init__(self, api):
        self.api = api
        self.app_edits = []
        self.agent_launches = []

    def __call__(self, args, **kwargs):
        if args[0] == "controlled-app-gh.exe":
            self.app_edits.append(list(args))
            remove = [args[i + 1] for i, value in enumerate(args[:-1]) if value == "--remove-label"]
            add = [args[i + 1] for i, value in enumerate(args[:-1]) if value == "--add-label"]
            self.api.issue["labels"] = [label for label in self.api.issue["labels"]
                                         if label["name"] not in remove]
            self.api.issue["labels"].extend({"name": label} for label in add)
            self.api.issue["updated_at"] += "+"
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if args[0] == "language_server.exe":
            self.agent_launches.append(list(args))
            return SimpleNamespace(returncode=0, stdout=json.dumps({"conversation_id": CONVERSATION_ID}), stderr="")
        raise AssertionError(f"unexpected production command: {args[0]}")


def init_git_repo(path):
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", str(path)], check=True, capture_output=True, text=True)
    subprocess.run(["git", "-C", str(path), "-c", "user.name=VCCP Test", "-c",
                    "user.email=vccp-test@example.invalid", "commit", "--allow-empty", "-m", "baseline"],
                   check=True, capture_output=True, text=True)
    return path


def linked_api(review=True):
    api = FakeAPI()
    api.canonical_events = [{"willCloseTarget": True,
                             "source": {"__typename": "PullRequest", "number": 12}}]
    api.pr = {"number": 12, "state": "open", "base": {"ref": "main"}, "user": {"login": "agent"},
              "head": {"sha": SHA, "ref": "implement/12"}}
    api.reviews = ([{"id": 4, "state": "CHANGES_REQUESTED", "commit_id": SHA, "submitted_at": "2026-01-01"}]
                   if review else [])
    api.issue["labels"] = [{"name": x} for x in ["changes-requested", "frozen-spec"]]
    return api


class AdapterTests(unittest.TestCase):
    def test_fresh_issue_maps_and_fail_closed_blockers(self):
        api = FakeAPI(); writer = MutatingWriter(api)
        adapter = GitHubWorkflowAdapter(api, writer, MANIFEST)
        snap = adapter.observe("o/r", 1)
        self.assertTrue(snap.issue_open and snap.frozen_spec)
        self.assertEqual(snap.coordination_state, "agent-ready")
        api.issue["body"] = "completion_policy: allow_no_change=true\nTask"
        authorized = adapter.observe("o/r", 1)
        self.assertTrue(authorized.allow_no_change)
        api.issue["body"] = "Task says no changes are needed."
        self.assertFalse(adapter.observe("o/r", 1).allow_no_change)
        api.issue["body"] = "completion_policy: allow_no_change=true\ncompletion_policy: allow_no_change=true"
        self.assertFalse(adapter.observe("o/r", 1).allow_no_change)
        api.issue["body"] = ""
        api.issue["state"] = "closed"
        self.assertFalse(adapter.observe("o/r", 1).issue_open)
        api.issue["state"] = "open"
        api.issue["labels"].remove({"name": "frozen-spec"})
        self.assertFalse(adapter.observe("o/r", 1).frozen_spec)

    def test_only_native_linkage_and_exact_head_review_are_exposed(self):
        api = linked_api()
        snap = GitHubWorkflowAdapter(api, FakeWriter(), MANIFEST).observe("o/r", 1)
        self.assertEqual((snap.pr_number, snap.pr_branch, snap.pr_head_sha), (12, "implement/12", SHA))
        self.assertEqual((snap.formal_review_state, snap.formal_review_id, snap.formal_review_head_sha),
                         ("CHANGES_REQUESTED", "4", SHA))
        api.reviews[0]["commit_id"] = "b" * 40
        self.assertIsNone(GitHubWorkflowAdapter(api, FakeWriter(), MANIFEST).observe("o/r", 1).formal_review_id)
        api.reviews = [{"id": 4, "state": "CHANGES_REQUESTED", "commit_id": SHA, "submitted_at": "1"},
                       {"id": 5, "state": "APPROVED", "commit_id": SHA, "submitted_at": "2"}]
        latest = GitHubWorkflowAdapter(api, FakeWriter(), MANIFEST).observe("o/r", 1)
        self.assertEqual((latest.formal_review_state, latest.formal_review_id), ("APPROVED", "5"))
        api.canonical_events = []
        self.assertIsNone(GitHubWorkflowAdapter(api, FakeWriter(), MANIFEST).observe("o/r", 1).pr_number)

    def test_formal_reviews_are_read_until_the_last_page(self):
        api = linked_api()
        api.reviews = [{"id": index, "state": "COMMENTED", "commit_id": "b" * 40,
                        "submitted_at": str(index)} for index in range(100)]
        api.reviews.append({"id": 101, "state": "APPROVED", "commit_id": SHA,
                            "submitted_at": "101"})
        snapshot = GitHubWorkflowAdapter(api, FakeWriter(), MANIFEST).observe("o/r", 1)
        self.assertEqual(snapshot.formal_review_id, "101")
        self.assertEqual([path for path in api.reads if "/reviews?" in path], [
            "/repos/o/r/pulls/12/reviews?per_page=100&page=1",
            "/repos/o/r/pulls/12/reviews?per_page=100&page=2",
        ])

    def test_snapshot_exposes_only_exact_head_checks_and_native_merged_fact(self):
        api = linked_api(review=False)
        api.check_runs = [
            {"name": "Unit", "head_sha": "b" * 40, "status": "completed", "conclusion": "success"},
            {"name": "Unit", "head_sha": SHA, "status": "completed", "conclusion": "failure"},
        ]
        adapter = GitHubWorkflowAdapter(api, FakeWriter(), MANIFEST)
        snapshot = adapter.observe("o/r", 1)
        self.assertEqual([run["head_sha"] for run in snapshot.check_runs], [SHA])
        api.pr.update(state="closed", merged=True)
        api.issue["state"] = "closed"
        merged = adapter.observe("o/r", 1)
        self.assertTrue(merged.pr_merged)
        self.assertEqual(merged.pr_state, "closed")
        self.assertFalse(merged.issue_open)

    def test_merged_label_cleanup_removes_active_and_preserves_terminal_labels(self):
        api = linked_api(review=False)
        api.pr.update(state="closed", merged=True)
        api.issue["state"] = "closed"
        api.issue["labels"].append({"name": "needs-human"})
        writer = MutatingWriter(api)
        adapter = GitHubWorkflowAdapter(api, writer, MANIFEST)
        self.assertTrue(adapter.clear_active_coordination_labels("o/r", 1))
        labels = {item["name"] for item in api.issue["labels"]}
        self.assertNotIn("changes-requested", labels)
        self.assertIn("needs-human", labels)
        self.assertEqual(adapter.observe("o/r", 1).coordination_state, "needs-human")
        self.assertEqual(writer.calls[0][2:], (["changes-requested"], []))

    def test_cleanup_succeeds_when_the_last_active_label_is_removed(self):
        # The correct end state of a merged Issue is no coordination label at all, which
        # the global state reports as "invalid" - that is not failed cleanup.
        api = linked_api(review=False)
        api.pr.update(state="closed", merged=True)
        api.issue["state"] = "closed"
        writer = MutatingWriter(api)
        adapter = GitHubWorkflowAdapter(api, writer, MANIFEST)
        self.assertTrue(adapter.clear_active_coordination_labels("o/r", 1))
        self.assertEqual({item["name"] for item in api.issue["labels"]}, {"frozen-spec"})
        self.assertEqual(adapter.observe("o/r", 1).coordination_state, "invalid")
        self.assertEqual(writer.calls[0][2:], (["changes-requested"], []))

    def test_cleanup_is_idempotent_when_no_active_label_remains(self):
        api = linked_api(review=False)
        api.pr.update(state="closed", merged=True)
        api.issue["state"] = "closed"
        api.issue["labels"] = [{"name": "frozen-spec"}]
        writer = MutatingWriter(api)
        self.assertTrue(GitHubWorkflowAdapter(api, writer, MANIFEST)
                        .clear_active_coordination_labels("o/r", 1))
        self.assertEqual(writer.calls, [])

    def test_cleanup_fails_closed_when_the_writer_refuses(self):
        api = linked_api(review=False)
        api.issue["labels"].append({"name": "agent-working"})
        writer = MutatingWriter(api)
        writer.success = False
        self.assertFalse(GitHubWorkflowAdapter(api, writer, MANIFEST)
                         .clear_active_coordination_labels("o/r", 1))

    def test_cleanup_fails_closed_when_an_active_label_survives(self):
        api = linked_api(review=False)
        api.issue["labels"].append({"name": "agent-working"})
        # A writer that reports success without removing anything: the labels are the fact.
        self.assertFalse(GitHubWorkflowAdapter(api, FakeWriter(), MANIFEST)
                         .clear_active_coordination_labels("o/r", 1))
        self.assertEqual({item["name"] for item in api.issue["labels"]},
                         {"changes-requested", "frozen-spec", "agent-working"})

    def test_cleanup_removes_every_contradictory_active_label_before_succeeding(self):
        api = linked_api(review=False)
        api.issue["labels"] = [{"name": x} for x in
                               ["agent-ready", "agent-working", "changes-requested", "frozen-spec"]]
        writer = MutatingWriter(api)
        self.assertTrue(GitHubWorkflowAdapter(api, writer, MANIFEST)
                        .clear_active_coordination_labels("o/r", 1))
        self.assertEqual({item["name"] for item in api.issue["labels"]}, {"frozen-spec"})

    def test_ambiguous_wrong_base_and_author_fail_closed(self):
        for mutate in (lambda a: a.canonical_events.append({"willCloseTarget": True,
                           "source": {"__typename": "PullRequest", "number": 13}}),
                       lambda a: a.pr["base"].update(ref="other"),
                       lambda a: a.pr["user"].update(login="stranger")):
            api = linked_api(); mutate(api)
            snap = GitHubWorkflowAdapter(api, FakeWriter(), MANIFEST).observe("o/r", 1)
            self.assertFalse(snap.pr_open)
            self.assertIsNone(snap.formal_review_id)

    def test_plain_cross_reference_does_not_authorize_repair(self):
        api = linked_api()
        api.canonical_events = [{"willCloseTarget": False,
                                 "source": {"__typename": "PullRequest", "number": 12}}]
        snap = GitHubWorkflowAdapter(api, FakeWriter(), MANIFEST).observe("o/r", 1)
        self.assertEqual(snap.open_linked_pr_count, 0)
        self.assertFalse(snap.pr_open)
        self.assertIsNone(snap.pr_number)
        self.assertIn("willCloseTarget", api.query)

    def test_bound_pr_merge_is_re_read_after_the_issue_auto_closes(self):
        api = linked_api(review=False)
        api.merged_pr = {"number": 12, "state": "MERGED", "merged": True, "baseRefName": "main",
                         "headRefName": "implement/12", "headRefOid": SHA,
                         "closingIssuesReferences": {"nodes": [{"number": 1}]}}
        adapter = GitHubWorkflowAdapter(api, FakeWriter(), MANIFEST)
        self.assertEqual(adapter.merged_closing_pr("o/r", 1, 12)["headRefOid"], SHA)
        self.assertEqual(api.variables, {"owner": "o", "name": "r", "number": 12})
        # The moment the Issue auto-closes, the timeline reports the closing PR as a plain
        # cross-reference, so the Issue read alone can no longer show that it merged.
        api.canonical_events = [{"willCloseTarget": False,
                                 "source": {"__typename": "PullRequest", "number": 12}}]
        api.issue["state"] = "closed"
        self.assertIsNone(adapter.observe("o/r", 1).pr_number)
        self.assertEqual(adapter.merged_closing_pr("o/r", 1, 12)["headRefOid"], SHA)

    def test_bound_pr_merge_read_fails_closed(self):
        api = linked_api(review=False)
        api.merged_pr = {"number": 12, "state": "MERGED", "merged": True, "baseRefName": "main",
                         "headRefName": "implement/12", "headRefOid": SHA,
                         "closingIssuesReferences": {"nodes": [{"number": 1}]}}
        adapter = GitHubWorkflowAdapter(api, FakeWriter(), MANIFEST)
        for mutate in (lambda a: a.merged_pr.update(closingIssuesReferences={"nodes": [{"number": 2}]}),
                       lambda a: a.merged_pr.update(closingIssuesReferences={"nodes": []}),
                       lambda a: a.merged_pr.update(state="CLOSED", merged=False),
                       lambda a: a.merged_pr.update(state="OPEN", merged=False),
                       lambda a: a.merged_pr.update(baseRefName="release"),
                       lambda a: a.merged_pr.update(headRefName="main"),
                       lambda a: a.merged_pr.update(headRefOid="")):
            api.merged_pr = {"number": 12, "state": "MERGED", "merged": True, "baseRefName": "main",
                             "headRefName": "implement/12", "headRefOid": SHA,
                             "closingIssuesReferences": {"nodes": [{"number": 1}]}}
            mutate(api)
            self.assertIsNone(adapter.merged_closing_pr("o/r", 1, 12))
        api.merged_pr = None
        self.assertIsNone(adapter.merged_closing_pr("o/r", 1, 12))
        self.assertIsNone(adapter.merged_closing_pr("o/r", 1, 0))
        self.assertIsNone(adapter.merged_closing_pr("o/r", 1, None))

    def test_github_api_uses_authenticated_graphql_post(self):
        seen = []
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def read(self): return b'{"data":{"ok":true}}'
        api = GitHubAPI("read-token", opener=lambda request, **kwargs: (seen.append(request) or Response()))
        self.assertEqual(api.graphql("query { viewer { login } }", {}), {"ok": True})
        self.assertEqual(seen[0].get_method(), "POST")
        self.assertEqual(seen[0].full_url, "https://api.github.com/graphql")
        self.assertEqual(seen[0].get_header("Authorization"), "Bearer read-token")
        with self.assertRaises(ValueError): GitHubAPI("read-token", api_url="https://ghe.example/api/v3")
        custom = GitHubAPI("read-token", api_url="https://ghe.example/api/v3",
                           graphql_url="https://ghe.example/api/graphql",
                           opener=lambda request, **kwargs: (seen.append(request) or Response()))
        custom.graphql("query { viewer { login } }", {})
        self.assertEqual(seen[-1].full_url, "https://ghe.example/api/graphql")

    def test_transition_checks_revision_uses_app_writer_and_verifies(self):
        api = FakeAPI(); writer = MutatingWriter(api)
        adapter = GitHubWorkflowAdapter(api, writer, MANIFEST)
        self.assertFalse(adapter.transition_coordination_state("o/r", 1, "agent-ready", "agent-working", "old"))
        self.assertFalse(writer.calls)
        self.assertTrue(adapter.transition_coordination_state("o/r", 1, "agent-ready", "agent-working", "r1"))
        self.assertEqual(writer.calls[0][2:], (["agent-ready"], ["agent-working"]))
        mismatch_api = FakeAPI()
        mismatch = GitHubWorkflowAdapter(mismatch_api, FakeWriter(), MANIFEST)
        self.assertFalse(mismatch.transition_coordination_state("o/r", 1, "agent-ready", "agent-working", "r1"))

        self.assertTrue(adapter.transition_coordination_state("o/r", 1, "agent-working", "needs-human", "r1+"))
        terminal = adapter.observe("o/r", 1)
        self.assertEqual((terminal.coordination_state, terminal.terminal_labels), ("needs-human", ("needs-human",)))
        call_count = len(writer.calls)
        self.assertFalse(adapter.transition_coordination_state("o/r", 1, "needs-human", "agent-ready", terminal.revision))
        self.assertEqual(len(writer.calls), call_count)

        infra_api = FakeAPI()
        infra_api.issue["labels"] = [{"name": "agent-working"}, {"name": "frozen-spec"}]
        infra_adapter = GitHubWorkflowAdapter(infra_api, MutatingWriter(infra_api), MANIFEST)
        self.assertTrue(infra_adapter.transition_coordination_state("o/r", 1, "agent-working", "infra-blocked", "r1"))
        self.assertEqual(infra_adapter.observe("o/r", 1).terminal_labels, ("infra-blocked",))

    def test_budget_authorization_is_the_only_transition_that_clears_needs_human(self):
        api = linked_api()
        api.issue["labels"] = [{"name": "needs-human"}, {"name": "frozen-spec"}]
        writer = MutatingWriter(api)
        adapter = GitHubWorkflowAdapter(api, writer, MANIFEST)
        observed = adapter.observe("o/r", 1)
        self.assertEqual(observed.coordination_state, "needs-human")
        self.assertFalse(adapter.restore_repair_coordination_state("o/r", 1, "changes-requested", "stale"))
        self.assertFalse(writer.calls)
        self.assertTrue(adapter.restore_repair_coordination_state("o/r", 1, "changes-requested", "r1"))
        self.assertEqual([call[2:] for call in writer.calls],
                         [([], ["changes-requested"]), (["needs-human"], [])])
        self.assertEqual(adapter.observe("o/r", 1).coordination_state, "changes-requested")

    def test_budget_label_promotion_retries_after_target_added_but_terminal_removal_fails(self):
        class FailSecondWrite(MutatingWriter):
            def __init__(self, api):
                super().__init__(api); self.writes = 0
            def replace_labels(self, repo, issue, remove, add):
                self.writes += 1
                if self.writes == 2:
                    self.calls.append((repo, issue, remove, add))
                    return False
                return super().replace_labels(repo, issue, remove, add)

        api = linked_api()
        api.issue["labels"] = [{"name": "needs-human"}, {"name": "frozen-spec"}]
        writer = FailSecondWrite(api)
        adapter = GitHubWorkflowAdapter(api, writer, MANIFEST)
        self.assertFalse(adapter.restore_repair_coordination_state("o/r", 1, "changes-requested", "r1"))
        partial = adapter.observe("o/r", 1)
        self.assertEqual(partial.terminal_labels, ("needs-human",))
        self.assertEqual(partial.active_labels, ("changes-requested",))
        self.assertTrue(adapter.restore_repair_coordination_state(
            "o/r", 1, "changes-requested", partial.revision))
        complete = adapter.observe("o/r", 1)
        self.assertEqual((complete.coordination_state, complete.terminal_labels), ("changes-requested", ()))

    def test_recovery_discovery_queries_only_active_states(self):
        api = FakeAPI()
        api.discovery["agent-working"] = [{"number": 8}, {"number": 9, "pull_request": {}}]
        api.discovery["changes-requested"] = [{"number": 10}]
        adapter = GitHubWorkflowAdapter(api, FakeWriter(), MANIFEST)
        self.assertEqual(adapter.discover_active("o/r"), [8, 10])
        self.assertTrue(all("agent-ready" not in path for path in api.reads))
        self.assertEqual(len(api.reads), 2)

    def test_ready_discovery_is_separate_and_rejects_partial_scan(self):
        api = FakeAPI()
        api.discovery["agent-ready"] = [{"number": 12}, {"number": 14},
                                          {"number": 13, "pull_request": {}}]
        adapter = GitHubWorkflowAdapter(api, FakeWriter(), MANIFEST)
        self.assertEqual(adapter.discover_ready("o/r"), [12, 14])
        self.assertEqual(len(api.reads), 1)
        api.discovery["agent-ready"] = [{"number": n} for n in range(100)]
        with self.assertRaisesRegex(RuntimeError, "incomplete"):
            adapter.discover_ready("o/r")

    def test_real_core_recovery_uses_app_writer_for_infra_blocked(self):
        with tempfile.TemporaryDirectory() as temp:
            api = FakeAPI()
            api.issue["body"] = "completion_policy: allow_no_change=true\nTask"
            api.issue["labels"] = [{"name": "agent-working"}, {"name": "frozen-spec"}]
            api.discovery["agent-working"] = [{"number": 1}]
            commands = FakeProductionCommands(api)
            workspace = init_git_repo(Path(temp) / "repo")
            local = {"database_path": str(Path(temp) / "runtime.sqlite"), "workspace": str(workspace),
                     "owner_id": "owner", "antigravity_executable": "language_server.exe",
                     "app_gh_executable": "controlled-app-gh.exe",
                     "app_git_push_executable": "controlled-app-git-push.exe",
                     "github_read_token_env": "TOKEN", "recovery_timeout_seconds": 60}
            runtime = build_runtime(MANIFEST, local, api=api, runner=commands)
            result = runtime.core.reconcile_once("o/r", "owner")
            self.assertEqual(result["items"][0]["status"], "orphan_without_pr")
            self.assertEqual(runtime.workflow.observe("o/r", 1).coordination_state, "infra-blocked")
            self.assertEqual(len(commands.app_edits), 1)
            self.assertIn("--add-label", commands.app_edits[0])
            self.assertIn("infra-blocked", commands.app_edits[0])
            self.assertFalse(commands.agent_launches)

    def test_app_gh_writer_uses_native_issue_edit_arguments(self):
        seen = []
        writer = GitHubAppWriter("C:/controlled/app-gh.exe",
                                 runner=lambda args, **kwargs: (seen.append(args) or SimpleNamespace(returncode=0)))
        self.assertTrue(writer.replace_labels("o/r", 3, ["agent-ready"], ["agent-working"]))
        self.assertEqual(seen[0], ["C:/controlled/app-gh.exe", "issue", "edit", "3", "--repo", "o/r",
                                   "--remove-label", "agent-ready", "--add-label", "agent-working"])
        with self.assertRaises(ValueError): GitHubAppWriter("gh")

    def test_antigravity_tristate_and_secret_stripping(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace = init_git_repo(Path(temp) / "repo")
            calls, envs, events = [], [], []
            real_guard = WorkspaceWriteGuard()
            class RecordingGuard:
                def install(self, *args, **kwargs):
                    result = real_guard.install(*args, **kwargs)
                    events.append("guard-installed")
                    return result
            adapter = AntiGravityImplementer("language_server.exe", str(workspace), "app-gh", "app-git-push",
                                             environ={"GH_TOKEN": "secret", "GITHUB_APP_TOKEN": "app-secret",
                                                      "KEEP": "yes"},
                                             write_guard=RecordingGuard())
            req = LaunchRequest("o/r", 1, "attempt", "initial_dispatch")
            response = SimpleNamespace(returncode=0, stdout=json.dumps({"response": {
                "newConversation": {"conversationId": CONVERSATION_ID}}}))
            def runner(args, **kwargs):
                events.append("agentapi")
                calls.append(args); envs.append(kwargs["env"]); return response
            adapter.runner = runner
            result = adapter.launch(req)
            self.assertEqual((result.disposition, result.execution_id), (LaunchDisposition.CONFIRMED, CONVERSATION_ID))
            self.assertEqual(events, ["guard-installed", "agentapi"])
            self.assertEqual(calls[0][:3], ["language_server.exe", "agentapi", "new-conversation"])
            self.assertIn("Controlled GitHub writer: app-gh", calls[0][-1])
            self.assertIn("Controlled git push wrapper:", calls[0][-1])
            self.assertNotIn("GH_TOKEN", envs[0])
            self.assertNotIn("GITHUB_APP_TOKEN", envs[0])
            self.assertEqual(envs[0]["KEEP"], "yes")
            self.assertEqual(envs[0]["VCCP_APP_GH"], "app-gh")
            self.assertEqual(envs[0]["VCCP_APP_GIT_PUSH_BACKEND"], "app-git-push")
            self.assertTrue(Path(envs[0]["GH_CONFIG_DIR"]).is_dir())
            self.assertTrue(Path(envs[0]["VCCP_APP_GIT_PUSH"]).is_file())
            for index, response in enumerate([
                    SimpleNamespace(returncode=1, stdout=json.dumps({"conversationId": CONVERSATION_ID})),
                    SimpleNamespace(returncode=0, stdout="{}"),
                    SimpleNamespace(returncode=0, stdout="not-json"),
                    SimpleNamespace(returncode=0, stdout=json.dumps({"error": "failed", "response": {
                        "newConversation": {"conversationId": CONVERSATION_ID}}})),
                    SimpleNamespace(returncode=0, stdout='{"conversation_id":"conv_123"}'),
                    SimpleNamespace(returncode=0, stdout='{"conversation_id":"exec-1"}'),
                    SimpleNamespace(returncode=0, stdout=json.dumps({"id": CONVERSATION_ID}))]):
                adapter.runner = lambda args, **kw: response
                req_variant = LaunchRequest("o/r", 1, f"attempt-{index}", "initial_dispatch")
                self.assertEqual(adapter.launch(req_variant).disposition, LaunchDisposition.UNKNOWN)
            adapter.runner = lambda *a, **kw: (_ for _ in ()).throw(TimeoutError())
            self.assertEqual(adapter.launch(LaunchRequest("o/r", 1, "attempt-timeout", "initial_dispatch")).disposition,
                             LaunchDisposition.UNKNOWN)
            adapter.runner = lambda *a, **kw: (_ for _ in ()).throw(FileNotFoundError())
            self.assertEqual(adapter.launch(LaunchRequest("o/r", 1, "attempt-missing", "initial_dispatch")).disposition,
                             LaunchDisposition.DEFINITELY_NOT_STARTED)

    def test_antigravity_launch_injects_current_language_server_context(self):
        with tempfile.TemporaryDirectory() as temp:
            executable = Path(temp) / "language_server"
            executable.touch()
            workspace = init_git_repo(Path(temp) / "repo")
            calls = []
            response = SimpleNamespace(returncode=0, stdout=json.dumps({"conversation_id": CONVERSATION_ID}))
            context = {"ANTIGRAVITY_LS_ADDRESS": "127.0.0.1:54195",
                       "ANTIGRAVITY_CSRF_TOKEN": "ephemeral-csrf-token"}
            adapter = AntiGravityImplementer(str(executable), str(workspace), "app-gh", "app-git-push",
                                             runner=lambda args, **kw: (calls.append((args, kw)), response)[1],
                                             environ={"HOME": "/Users/test"})
            with mock.patch("vccp_runtime.adapters.sys.platform", "darwin"), \
                 mock.patch("vccp_runtime.adapters._discover_antigravity_agentapi_environment",
                            return_value=context) as discover:
                result = adapter.launch(LaunchRequest("o/r", 1, "attempt-context", "initial_dispatch"))

            self.assertEqual(result.disposition, LaunchDisposition.CONFIRMED)
            discover.assert_called_once_with(str(executable))
            child_env = calls[0][1]["env"]
            self.assertEqual(child_env["ANTIGRAVITY_LS_ADDRESS"], context["ANTIGRAVITY_LS_ADDRESS"])
            self.assertEqual(child_env["ANTIGRAVITY_CSRF_TOKEN"], context["ANTIGRAVITY_CSRF_TOKEN"])

    def test_antigravity_context_discovery_uses_only_healthy_loopback_server(self):
        from vccp_runtime.adapters import _discover_antigravity_agentapi_environment

        executable = "/Applications/Antigravity.app/Contents/Resources/bin/language_server"
        commands = []

        def fake_run(args, **kwargs):
            commands.append(args)
            if args[:3] == ["ps", "-axo", "pid=,command="]:
                return SimpleNamespace(returncode=0, stdout=(
                    f"510 {executable} --standalone --override_ide_name antigravity "
                    "--csrf_token=ephemeral-csrf-token\n"))
            if args[:3] == ["lsof", "-nP", "-a"]:
                return SimpleNamespace(returncode=0, stdout=(
                    "COMMAND PID USER FD TYPE DEVICE SIZE/OFF NODE NAME\n"
                    "language 510 user 7u IPv4 0x1 0t0 TCP 127.0.0.1:54195 (LISTEN)\n"
                    "language 510 user 8u IPv4 0x2 0t0 TCP *:54196 (LISTEN)\n"))
            self.fail(f"unexpected command: {args}")

        class HealthResponse:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *_): return False
            def read(self): return b'{"status":"ok"}'

        with mock.patch("vccp_runtime.adapters.sys.platform", "darwin"), \
             mock.patch("vccp_runtime.adapters.subprocess.run", side_effect=fake_run), \
             mock.patch("vccp_runtime.adapters.urlopen", return_value=HealthResponse()) as open_url:
            result = _discover_antigravity_agentapi_environment(executable)

        self.assertEqual(result, {"ANTIGRAVITY_LS_ADDRESS": "127.0.0.1:54195",
                                  "ANTIGRAVITY_CSRF_TOKEN": "ephemeral-csrf-token"})
        self.assertEqual(len(commands), 2)
        self.assertIn("http://127.0.0.1:54195/healthz", open_url.call_args.args[0].full_url)

    def _windows_discovery(self, processes, connections, healthy_ports=(), executable=None):
        """Drive _discover_antigravity_agentapi_environment through the Windows process table."""
        from vccp_runtime.adapters import _discover_antigravity_agentapi_environment

        executable = executable or r"C:\Program Files\Antigravity\resources\bin\language_server.exe"
        commands = []

        def fake_run(args, **kwargs):
            commands.append(args)
            if args[0] == "powershell.exe":
                return SimpleNamespace(returncode=0, stdout=json.dumps(processes))
            if args[0] == "netstat.exe":
                return SimpleNamespace(returncode=0, stdout="\n".join(connections))
            self.fail(f"unexpected command: {args}")

        def fake_urlopen(request, timeout=0):
            port = int(request.full_url.rsplit(":", 1)[1].split("/")[0])
            if port not in healthy_ports:
                raise OSError("connection refused")
            class Healthy:
                status = 200
                def __enter__(self): return self
                def __exit__(self, *_): return False
                def read(self): return b'{"status":"ok"}'
            return Healthy()

        with mock.patch("vccp_runtime.adapters.sys.platform", "win32"), \
             mock.patch("vccp_runtime.adapters.subprocess.run", side_effect=fake_run), \
             mock.patch("vccp_runtime.adapters.urlopen", side_effect=fake_urlopen):
            return _discover_antigravity_agentapi_environment(executable), commands, executable

    def test_windows_discovery_finds_the_running_language_server(self):
        executable = r"C:\Program Files\Antigravity\resources\bin\language_server.exe"
        result, commands, _ = self._windows_discovery(
            processes=[{
                "ProcessId": 22328,
                "ExecutablePath": executable,
                "CommandLine": f'"{executable}" --standalone --override_ide_name antigravity '
                               f'--https_server_port 0 --csrf_token ephemeral-csrf-token',
            }],
            connections=[
                "  TCP    127.0.0.1:7894         0.0.0.0:0              LISTENING       22328",
                "  TCP    127.0.0.1:7895         0.0.0.0:0              LISTENING       22328",
                "  TCP    127.0.0.1:7895         127.0.0.1:50123        ESTABLISHED     99999",
            ],
            healthy_ports={7895},
        )

        self.assertEqual(result, {"ANTIGRAVITY_LS_ADDRESS": "127.0.0.1:7895",
                                  "ANTIGRAVITY_CSRF_TOKEN": "ephemeral-csrf-token"})
        # Only the CIM query and one netstat pass are issued; no ps/lsof on Windows.
        self.assertEqual([command[0] for command in commands], ["powershell.exe", "netstat.exe"])

    def test_windows_discovery_ignores_other_processes_and_unhealthy_ports(self):
        executable = r"C:\Program Files\Antigravity\resources\bin\language_server.exe"
        result, _, _ = self._windows_discovery(
            processes=[{
                "ProcessId": 4310,
                "ExecutablePath": r"C:\Other\language_server.exe",
                "CommandLine": r'"C:\Other\language_server.exe" --standalone --override_ide_name antigravity '
                               r"--csrf_token other-token",
            }, {
                "ProcessId": 22328,
                "ExecutablePath": executable,
                "CommandLine": f'"{executable}" --standalone --override_ide_name antigravity '
                               f"--csrf_token ephemeral-csrf-token",
            }],
            connections=["  TCP    127.0.0.1:7895         0.0.0.0:0              LISTENING       22328"],
            healthy_ports=set(),
        )

        self.assertIsNone(result)

    def test_windows_discovery_reports_ambiguity_instead_of_guessing(self):
        executable = r"C:\Program Files\Antigravity\resources\bin\language_server.exe"

        def process(pid, token):
            return {"ProcessId": pid, "ExecutablePath": executable,
                    "CommandLine": f'"{executable}" --standalone --override_ide_name antigravity '
                                   f"--csrf_token {token}"}

        result, _, _ = self._windows_discovery(
            processes=[process(100, "token-a"), process(200, "token-b")],
            connections=[
                "  TCP    127.0.0.1:7895         0.0.0.0:0              LISTENING       100",
                "  TCP    127.0.0.1:7896         0.0.0.0:0              LISTENING       200",
            ],
            healthy_ports={7895, 7896},
        )

        self.assertIsNone(result)

    def test_antigravity_launch_discovers_context_on_windows(self):
        with tempfile.TemporaryDirectory() as temp:
            executable = Path(temp) / "language_server.exe"
            executable.touch()
            workspace = init_git_repo(Path(temp) / "repo")
            calls = []
            response = SimpleNamespace(returncode=0, stdout=json.dumps({"conversation_id": CONVERSATION_ID}))
            context = {"ANTIGRAVITY_LS_ADDRESS": "127.0.0.1:7895",
                       "ANTIGRAVITY_CSRF_TOKEN": "ephemeral-csrf-token"}
            adapter = AntiGravityImplementer(str(executable), str(workspace), "app-gh", "app-git-push",
                                             runner=lambda args, **kw: (calls.append((args, kw)), response)[1],
                                             environ={"USERPROFILE": temp})
            with mock.patch("vccp_runtime.adapters.sys.platform", "win32"), \
                 mock.patch("vccp_runtime.adapters._discover_antigravity_agentapi_environment",
                            return_value=context) as discover:
                result = adapter.launch(LaunchRequest("o/r", 1, "attempt-win", "initial_dispatch"))

            self.assertEqual(result.disposition, LaunchDisposition.CONFIRMED)
            discover.assert_called_once_with(str(executable))
            child_env = calls[0][1]["env"]
            self.assertEqual(child_env["ANTIGRAVITY_LS_ADDRESS"], context["ANTIGRAVITY_LS_ADDRESS"])
            self.assertEqual(child_env["ANTIGRAVITY_CSRF_TOKEN"], context["ANTIGRAVITY_CSRF_TOKEN"])

    def test_antigravity_launch_prefers_explicit_environment_over_discovery(self):
        with tempfile.TemporaryDirectory() as temp:
            executable = Path(temp) / "language_server.exe"
            executable.touch()
            workspace = init_git_repo(Path(temp) / "repo")
            calls = []
            response = SimpleNamespace(returncode=0, stdout=json.dumps({"conversation_id": CONVERSATION_ID}))
            explicit = {"ANTIGRAVITY_LS_ADDRESS": "127.0.0.1:60001",
                        "ANTIGRAVITY_CSRF_TOKEN": "explicit-token"}
            for platform in ("darwin", "win32"):
                adapter = AntiGravityImplementer(str(executable), str(workspace), "app-gh", "app-git-push",
                                                 runner=lambda args, **kw: (calls.append((args, kw)), response)[1],
                                                 environ=dict(explicit))
                with mock.patch("vccp_runtime.adapters.sys.platform", platform), \
                     mock.patch("vccp_runtime.adapters._discover_antigravity_agentapi_environment") as discover:
                    result = adapter.launch(LaunchRequest("o/r", 1, f"attempt-explicit-{platform}",
                                                          "initial_dispatch"))
                self.assertEqual(result.disposition, LaunchDisposition.CONFIRMED)
                discover.assert_not_called()

            child_env = calls[0][1]["env"]
            self.assertEqual(child_env["ANTIGRAVITY_LS_ADDRESS"], explicit["ANTIGRAVITY_LS_ADDRESS"])
            self.assertEqual(child_env["ANTIGRAVITY_CSRF_TOKEN"], explicit["ANTIGRAVITY_CSRF_TOKEN"])

    def test_antigravity_launch_never_invokes_agentapi_without_discoverable_context_on_windows(self):
        with tempfile.TemporaryDirectory() as temp:
            executable = Path(temp) / "language_server.exe"
            executable.touch()
            workspace = init_git_repo(Path(temp) / "repo")
            calls = []
            adapter = AntiGravityImplementer(str(executable), str(workspace), "app-gh", "app-git-push",
                                             runner=lambda *a, **kw: calls.append(a),
                                             environ={"USERPROFILE": temp})
            with mock.patch("vccp_runtime.adapters.sys.platform", "win32"), \
                 mock.patch("vccp_runtime.adapters._discover_antigravity_agentapi_environment",
                            return_value=None):
                result = adapter.launch(LaunchRequest("o/r", 1, "attempt-win-missing", "initial_dispatch"))

            self.assertEqual(result.disposition, LaunchDisposition.DEFINITELY_NOT_STARTED)
            self.assertEqual(calls, [])

    def test_guard_failure_prevents_agentapi_invocation(self):
        calls = []
        class FailedGuard:
            def install(self, *args): raise RuntimeError("cannot install guard")
        adapter = AntiGravityImplementer("language_server.exe", "workspace", "app-gh", "app-git-push",
                                         runner=lambda *a, **kw: calls.append(a), write_guard=FailedGuard())
        result = adapter.launch(LaunchRequest("o/r", 1, "attempt-failed", "initial_dispatch"))
        self.assertEqual(result.disposition, LaunchDisposition.DEFINITELY_NOT_STARTED)
        self.assertEqual(calls, [])

    def test_implementer_session_project_isolation_and_env_sanitization(self):
        with tempfile.TemporaryDirectory() as temp:
            temp_dir = Path(temp)
            projects_dir = temp_dir / "projects"
            projects_dir.mkdir()
            ws_a = init_git_repo(temp_dir / "consumer_a")
            ws_b = init_git_repo(temp_dir / "consumer_b")
            ws_c = init_git_repo(temp_dir / "consumer_unregistered")

            # Register projects A and B with file:// URIs
            uri_a = f"file:///{str(ws_a).replace(chr(92), '/')}"
            uri_b = f"file:///{str(ws_b).replace(chr(92), '/')}"
            (projects_dir / "proj_a.json").write_text(json.dumps({
                "id": "proj-id-alpha",
                "name": "consumer_a",
                "projectResources": {"resources": [{"gitFolder": {"folderUri": uri_a}}]}
            }), encoding="utf-8")
            (projects_dir / "proj_b.json").write_text(json.dumps({
                "id": "proj-id-beta",
                "name": "consumer_b",
                "projectResources": {"resources": [{"gitFolder": {"folderUri": uri_b}}]}
            }), encoding="utf-8")

            parent_env = {
                "ANTIGRAVITY_PROJECT_ID": "parent-vccp-control-plane-id",
                "ANTIGRAVITY_CONVERSATION_ID": "parent-runner-conversation-id",
                "ANTIGRAVITY_SOURCE_METADATA": "{\"tool\":{}}",
                "ANTIGRAVITY_TRAJECTORY_ID": "parent-traj-id",
                "CUSTOM_RUNNER_VAR": "runner-val",
            }
            dummy_resp = SimpleNamespace(
                returncode=0,
                stdout=json.dumps({"response": {"newConversation": {"conversationId": CONVERSATION_ID}}})
            )

            # Case 1: Consumer A -> Should resolve to proj-id-alpha and clear conversation ID
            recorded_calls = []
            adapter_a = AntiGravityImplementer(
                "language_server.exe", str(ws_a), "app-gh", "app-git-push",
                environ=parent_env,
                runner=lambda args, **kw: (recorded_calls.append((args, kw)), dummy_resp)[1],
                project_config_root=projects_dir,
            )
            res_a = adapter_a.launch(LaunchRequest("o/r", 1, "attempt-a", "initial_dispatch"))
            self.assertEqual(res_a.disposition, LaunchDisposition.CONFIRMED)
            self.assertEqual(recorded_calls[0][1]["cwd"], str(ws_a))
            env_a = recorded_calls[0][1]["env"]
            self.assertEqual(env_a.get("ANTIGRAVITY_PROJECT_ID"), "proj-id-alpha")
            self.assertNotIn("ANTIGRAVITY_CONVERSATION_ID", env_a)
            self.assertNotIn("ANTIGRAVITY_SOURCE_METADATA", env_a)
            self.assertNotIn("ANTIGRAVITY_TRAJECTORY_ID", env_a)
            self.assertEqual(env_a.get("CUSTOM_RUNNER_VAR"), "runner-val")

            # Case 2: Consumer B -> Should resolve to proj-id-beta
            recorded_calls.clear()
            adapter_b = AntiGravityImplementer(
                "language_server.exe", str(ws_b), "app-gh", "app-git-push",
                environ=parent_env,
                runner=lambda args, **kw: (recorded_calls.append((args, kw)), dummy_resp)[1],
                project_config_root=projects_dir,
            )
            res_b = adapter_b.launch(LaunchRequest("o/r", 2, "attempt-b", "initial_dispatch"))
            self.assertEqual(res_b.disposition, LaunchDisposition.CONFIRMED)
            self.assertEqual(recorded_calls[0][1]["cwd"], str(ws_b))
            env_b = recorded_calls[0][1]["env"]
            self.assertEqual(env_b.get("ANTIGRAVITY_PROJECT_ID"), "proj-id-beta")
            self.assertNotIn("ANTIGRAVITY_CONVERSATION_ID", env_b)
            self.assertNotIn("ANTIGRAVITY_SOURCE_METADATA", env_b)
            self.assertNotIn("ANTIGRAVITY_TRAJECTORY_ID", env_b)

            # Case 3: Unregistered Consumer C -> Must strip ANTIGRAVITY_PROJECT_ID, NOT leak parent VCCP ID
            recorded_calls.clear()
            adapter_c = AntiGravityImplementer(
                "language_server.exe", str(ws_c), "app-gh", "app-git-push",
                environ=parent_env,
                runner=lambda args, **kw: (recorded_calls.append((args, kw)), dummy_resp)[1],
                project_config_root=projects_dir,
            )
            res_c = adapter_c.launch(LaunchRequest("o/r", 3, "attempt-c", "initial_dispatch"))
            self.assertEqual(res_c.disposition, LaunchDisposition.CONFIRMED)
            self.assertEqual(recorded_calls[0][1]["cwd"], str(ws_c))
            env_c = recorded_calls[0][1]["env"]
            self.assertNotIn("ANTIGRAVITY_PROJECT_ID", env_c)
            self.assertNotIn("ANTIGRAVITY_CONVERSATION_ID", env_c)
            self.assertNotIn("ANTIGRAVITY_SOURCE_METADATA", env_c)
            self.assertNotIn("ANTIGRAVITY_TRAJECTORY_ID", env_c)

    def test_workspace_guard_blocks_direct_push_without_tracked_changes(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace = init_git_repo(Path(temp) / "repo")
            remote = Path(temp) / "remote.git"
            subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True, text=True)
            subprocess.run(["git", "-C", str(workspace), "remote", "add", "origin", str(remote)],
                           check=True, capture_output=True)
            before = subprocess.run(["git", "status", "--porcelain"], cwd=workspace, capture_output=True, text=True)
            guard = WorkspaceWriteGuard().install(str(workspace), "guarded-attempt", shutil.which("git"),
                                                  provider="antigravity")
            env = dict(os.environ); env.update(guard.environment)
            blocked = subprocess.run(["git", "push", "origin", "HEAD"], cwd=workspace,
                                     capture_output=True, text=True, timeout=10)
            after = subprocess.run(["git", "status", "--porcelain"], cwd=workspace, capture_output=True, text=True)
            self.assertNotEqual(blocked.returncode, 0)
            self.assertIn("Direct git push is disabled", blocked.stderr)
            self.assertEqual(before.stdout, after.stdout)
            self.assertTrue(Path(guard.git_push_command).is_file())
            wrapper = Path(guard.git_push_command).read_text(encoding="utf-8")
            self.assertIn(Path(shutil.which("git")).name, wrapper)
            self.assertIn("VCCP_CONTROLLED_PUSH=1", wrapper)
            self.assertEqual(list(Path(guard.environment["GH_CONFIG_DIR"]).iterdir()), [])
            if os.name == "nt":
                controlled_command = ["cmd", "/c", guard.git_push_command, "push", "origin", "HEAD"]
            else:
                controlled_command = [guard.git_push_command, "push", "origin", "HEAD"]
            controlled = subprocess.run(controlled_command, cwd=workspace, env=env,
                                        capture_output=True, text=True, timeout=10)
            self.assertEqual(controlled.returncode, 0, controlled.stderr)

    def test_prompt_generic_and_repair_is_exact(self):
        prompt = generic_prompt(LaunchRequest("o/r", 3, "a", "repair", repair_ordinal=2,
            expected_head_sha=SHA, pr_number=12, branch="fix/12"), "local", MANIFEST,
            "C:/tools/app-gh.exe", "C:/tools/app-git-push.exe")
        self.assertIn("existing PR #12", prompt); self.assertIn(SHA, prompt)
        self.assertIn("ordinal 2 is authoritative", prompt)
        self.assertIn("Frozen Issue: #3", prompt)
        self.assertIn("ordinary host-human gh writes are forbidden", prompt.lower())
        self.assertIn("ordinary git push is forbidden", prompt.lower())
        self.assertIn("Closes #<issue_number>", prompt)
        self.assertIn("a plain mention is insufficient", prompt)
        self.assertIn("--body 'SUMMARY. Closes #<issue_number>'", prompt)
        self.assertIn("C:/tools/app-git-push.exe", prompt)
        noop_prompt = generic_prompt(LaunchRequest("o/r", 3, "b", "initial_dispatch",
            allow_no_change=True, completion_command="/git/complete-no-change"), "local", MANIFEST,
            "app-gh", "app-git-push")
        self.assertIn("`/git/complete-no-change`", noop_prompt)
        self.assertIn("Otherwise, implement", noop_prompt)
        self.assertNotIn("and open one PR", noop_prompt.split("Otherwise,")[0])
        for forbidden in ("EasyExam", "Reviewer-only", "three REQUEST_CHANGES rounds"):
            self.assertNotIn(forbidden, prompt)

    def test_prompt_embeds_frozen_spec_and_only_rejection_repairs_get_exact_head_feedback(self):
        spec = "Goal: export rows. Acceptance: a row containing a comma stays one row."
        initial = generic_prompt(LaunchRequest("o/r", 3, "a", "initial_dispatch",
                                               frozen_spec_body=spec), "local", MANIFEST,
                                 "app-gh", "app-git-push")
        self.assertIn("<frozen-spec>\n" + spec + "\n</frozen-spec>", initial)
        self.assertNotIn("<review-findings>", initial)

        rejection = generic_prompt(LaunchRequest(
            "o/r", 3, "b", "repair", repair_cause_type="reviewer_rejection",
            review_feedback="Criterion 2 still fails: `export.py:88` drops quoted commas.",
            frozen_spec_body=spec), "local", MANIFEST, "app-gh", "app-git-push")
        self.assertIn("<review-findings>\nCriterion 2 still fails: `export.py:88` drops quoted "
                      "commas.\n</review-findings>", rejection)
        self.assertIn("Handle only findings this review produced", rejection)
        self.assertIn("the Frozen Issue wins", rejection)

        ci = generic_prompt(LaunchRequest(
            "o/r", 3, "c", "repair", repair_cause_type="implementation_failure",
            review_feedback="review from an older head that must not authorize this repair"),
            "local", MANIFEST, "app-gh", "app-git-push")
        self.assertNotIn("<review-findings>", ci)
        self.assertNotIn("review from an older head", ci)

        missing = generic_prompt(LaunchRequest("o/r", 3, "d", "initial_dispatch"),
                                 "local", MANIFEST, "app-gh", "app-git-push")
        self.assertIn("could not be supplied", missing)
        self.assertNotIn("<frozen-spec>", missing)

    def test_runtime_wiring_real_core_initial_and_schema_guard(self):
        with tempfile.TemporaryDirectory() as temp:
            api = FakeAPI(); launches = []
            workspace = init_git_repo(Path(temp) / "repo")
            def runner(args, **kwargs):
                self.assertEqual(args[:2], ["language_server.exe", "agentapi"])
                launches.append(args[-1])
                return SimpleNamespace(returncode=0, stdout=json.dumps({"conversation_id": CONVERSATION_ID}))
            local = {"database_path": str(Path(temp)/"db.sqlite"), "workspace": str(workspace), "owner_id": "owner",
                     "antigravity_executable": "language_server.exe", "app_gh_executable": "app-gh.exe",
                     "app_git_push_executable": "app-git-push.exe", "github_read_token_env": "TOKEN"}
            wired = build_runtime(MANIFEST, local, api=api, writer=MutatingWriter(api), runner=runner)
            self.assertIsInstance(wired.lifecycle, LifecycleDriver)
            result = wired.core.dispatch_initial("o/r", 1, "owner")
            self.assertEqual(result["phase"], "LAUNCH_CONFIRMED")
            self.assertEqual(Path(wired.core.workspace_path).resolve(), Path(workspace).resolve())
            self.assertEqual(len(launches), 1)
            with self.assertRaises(ValueError): build_runtime({"schema_version": 1}, local, api, FakeWriter(), runner)

    def test_runtime_wires_explicit_independent_local_reviewer_provider(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace = init_git_repo(Path(temp) / "repo")
            subprocess.run(["git", "-C", str(workspace), "remote", "add", "origin",
                            "https://github.com/o/r.git"], check=True, capture_output=True)
            local = {
                "database_path": str(Path(temp) / "runtime.sqlite"), "workspace": str(workspace),
                "owner_id": "owner", "target_repository": "o/r",
                "github_app": {"app_id": "1", "credential_source": {"type": "test"}},
                "providers": [
                    {"key": "antigravity", "type": "antigravity", "executable": "language_server"},
                    {"key": "claude_agent_sdk", "type": "claude_agent_sdk"},
                ],
                "implementer_provider": "antigravity", "reviewer_provider": "claude_agent_sdk",
                "reviewer_github_identity": "liuchangchxy",
            }
            class CredentialProvider:
                api_url = "https://api.github.com"
            wired = build_runtime(MANIFEST, local, api=FakeAPI(), writer=FakeWriter(),
                                  credential_provider=CredentialProvider(), push_backend=object())

            from vccp_runtime.reviewer_agents import ClaudeAgentSDKReviewer
            self.assertEqual(wired.router.primary, "antigravity")
            self.assertEqual(wired.local_reviewer.provider_key, "claude_agent_sdk")
            self.assertIsInstance(wired.local_reviewer.provider, ClaudeAgentSDKReviewer)
            self.assertIs(wired.lifecycle.local_reviewer, wired.local_reviewer)

    def test_generic_app_runtime_needs_no_prebuilt_helpers_and_generates_attempt_wrappers(self):
        with tempfile.TemporaryDirectory() as temp:
            api = FakeAPI()
            api.issue["body"] = "completion_policy: allow_no_change=true\nTask"
            workspace = init_git_repo(Path(temp) / "repo")
            subprocess.run(["git", "-C", str(workspace), "remote", "add", "origin",
                            "https://github.com/o/r.git"], check=True, capture_output=True)

            class Provider:
                api_url = "https://api.github.com"
                target_repository = "o/r"
                def get_token(self, repo): raise AssertionError("launch must not mint a token")
                def request(self, *args, **kwargs): raise AssertionError("launch must not call GitHub")

            class PushBackend:
                def push(self, *args, **kwargs): raise AssertionError("construction must not push")

            local = {"database_path": str(Path(temp) / "runtime.sqlite"), "workspace": str(workspace),
                     "owner_id": "owner", "antigravity_executable": "language_server.exe",
                     "github_app": {"app_id": "123456", "credential_source": {
                         "type": "windows_dpapi_file", "path": str(Path(temp) / "key.dpapi")}}}
            provider = Provider()
            launches = []
            def agent_runner(args, **kwargs):
                launches.append((args, kwargs))
                return SimpleNamespace(returncode=0, stdout=json.dumps({"conversation_id": CONVERSATION_ID}))
            runtime = build_runtime(MANIFEST, local, api=api, writer=MutatingWriter(api),
                                    runner=agent_runner, credential_provider=provider,
                                    push_backend=PushBackend())
            result = runtime.core.dispatch_initial("o/r", 1, "owner")
            self.assertEqual(result["phase"], "LAUNCH_CONFIRMED")
            self.assertIn(".cmd" if os.name == "nt" else "app-gh", launches[0][0][-1])
            self.assertIn("app-git-push", launches[0][0][-1])
            self.assertNotIn("app_gh_executable", local)
            self.assertNotIn("app_git_push_executable", local)
            self.assertNotIn("github_read_token_env", local)
            env = launches[0][1]["env"]
            self.assertEqual(env["VCCP_APP_GIT_PUSH_BACKEND"], "vccp_runtime.github_push.GitHubAppPushBackend")
            self.assertNotIn("GITHUB_APP_PRIVATE_KEY", env)
            completion = list(Path(subprocess.run(["git", "-C", str(workspace), "rev-parse", "--absolute-git-dir"],
                                                   capture_output=True, text=True, check=True).stdout.strip())
                              .glob("vccp-control/*/*/complete-no-change*"))[0]
            self.assertTrue(os.access(completion, os.X_OK))
            self.assertIn(str(completion), launches[0][0][-1])
            attempt = runtime.core.store.attempt(result["attempt_id"])
            self.assertNotEqual(attempt["workspace_baseline"], "{}")
            self.assertTrue(list(Path(subprocess.run(["git", "-C", str(workspace), "rev-parse", "--absolute-git-dir"],
                                                     capture_output=True, text=True, check=True).stdout.strip())
                               .glob("vccp-control/*/*/app-gh*")))

    def test_production_completion_wrapper_records_receipt_and_reconciles(self):
        with tempfile.TemporaryDirectory() as temp:
            api = FakeAPI()
            api.issue["body"] = "completion_policy: allow_no_change=true\nFrozen task"
            workspace = init_git_repo(Path(temp) / "repo")
            subprocess.run(["git", "-C", str(workspace), "remote", "add", "origin",
                            "https://github.com/o/r.git"], check=True, capture_output=True)

            class Provider:
                api_url = "https://api.github.com"
                target_repository = "o/r"
                def get_token(self, repo): raise AssertionError("launch must not mint a token")
                def request(self, *args, **kwargs): raise AssertionError("launch must not call GitHub")

            class PushBackend:
                def push(self, *args, **kwargs): raise AssertionError("completion must not push")

            class ClosingWriter(MutatingWriter):
                def close_issue(self, repo, issue):
                    self.api.issue["state"] = "closed"
                    return True

            launches = []
            def agent_runner(args, **kwargs):
                launches.append((args, kwargs))
                return SimpleNamespace(returncode=0, stdout=json.dumps({"conversation_id": CONVERSATION_ID}))

            local = {"database_path": str(Path(temp) / "runtime.sqlite"), "workspace": str(workspace),
                     "owner_id": "worker", "antigravity_executable": "language_server.exe",
                     "github_app": {"app_id": "123456", "credential_source": {
                         "type": "windows_dpapi_file", "path": str(Path(temp) / "key.dpapi")}}}
            runtime = build_runtime(MANIFEST, local, api=api, writer=ClosingWriter(api),
                                    runner=agent_runner, credential_provider=Provider(), push_backend=PushBackend())
            result = runtime.core.dispatch_initial("o/r", 1, "worker")
            attempt = runtime.core.store.attempt(result["attempt_id"])
            completion = list(Path(subprocess.run(
                ["git", "-C", str(workspace), "rev-parse", "--absolute-git-dir"],
                capture_output=True, text=True, check=True).stdout.strip()
            ).glob("vccp-control/*/*/complete-no-change*"))[0]
            runtime_context = json.loads(completion.parent.joinpath("runtime-context.json").read_text())
            self.assertEqual(runtime_context["workspace_baseline"], attempt["workspace_baseline"])
            env = dict(launches[0][1]["env"])
            self.assertIn(str(Path(__file__).resolve().parents[2]), env["PYTHONPATH"].split(os.pathsep))
            self.assertIsNone(runtime.router._by_key["antigravity"].launch_context_override)
            command = [str(completion)] if os.name != "nt" else ["cmd", "/c", str(completion)]
            completed = subprocess.run(command, cwd=workspace, env=env, capture_output=True, text=True)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            receipt = runtime.core.store.receipt(result["attempt_id"])
            self.assertIsNotNone(receipt)
            self.assertEqual(receipt["workspace_evidence"], attempt["workspace_baseline"])
            reconciliation = runtime.core.reconcile_no_change("o/r")
            self.assertEqual(reconciliation["items"][0]["status"], "success_no_op")
            self.assertEqual(runtime.core.store.attempt(result["attempt_id"])["phase"], "SUCCESS_NO_OP")

    def test_controlled_push_blocks_noop_after_workspace_is_restored(self):
        with tempfile.TemporaryDirectory() as temp:
            api = FakeAPI()
            api.issue["body"] = "completion_policy: allow_no_change=true\nFrozen task"
            workspace = init_git_repo(Path(temp) / "repo")
            subprocess.run(["git", "-C", str(workspace), "remote", "add", "origin",
                            "https://github.com/o/r.git"], check=True, capture_output=True)

            class Provider:
                api_url = "https://api.github.com"
                target_repository = "o/r"
                def get_token(self, repo): raise AssertionError("mocked push must not mint a token")
                def request(self, *args, **kwargs): raise AssertionError("mocked push must not call GitHub")

            class PushBackend:
                def push(self, *args, **kwargs):
                    self.args = args
                    self.kwargs = kwargs
                    return {"status": "pushed", "sha": args[2], "destination_ref": args[3]}

            launches = []
            def agent_runner(args, **kwargs):
                launches.append(kwargs)
                return SimpleNamespace(returncode=0, stdout=json.dumps({"conversation_id": CONVERSATION_ID}))

            local = {"database_path": str(Path(temp) / "runtime.sqlite"), "workspace": str(workspace),
                     "owner_id": "worker", "antigravity_executable": "language_server.exe",
                     "github_app": {"app_id": "123456", "credential_source": {
                         "type": "windows_dpapi_file", "path": str(Path(temp) / "key.dpapi")}}}
            runtime = build_runtime(MANIFEST, local, api=api, writer=MutatingWriter(api),
                                    runner=agent_runner, credential_provider=Provider(), push_backend=object())
            result = runtime.core.dispatch_initial("o/r", 1, "worker")
            attempt = runtime.core.store.attempt(result["attempt_id"])
            git_dir = Path(subprocess.run(["git", "-C", str(workspace), "rev-parse", "--absolute-git-dir"],
                                          capture_output=True, text=True, check=True).stdout.strip())
            context_path = list(git_dir.glob("vccp-control/*/*/runtime-context.json"))[0]
            completion = list(git_dir.glob("vccp-control/*/*/complete-no-change*"))[0]
            head = subprocess.run(["git", "-C", str(workspace), "rev-parse", "HEAD"],
                                  capture_output=True, text=True, check=True).stdout.strip()
            backend = PushBackend()
            with mock.patch.dict(os.environ, {"VCCP_CONTROLLED_PUSH": "1"}), \
                 mock.patch.object(controlled_cli, "_provider", return_value=Provider()), \
                 mock.patch.object(controlled_cli, "GitHubAppPushBackend", return_value=backend):
                pushed = controlled_cli.run(["--context", str(context_path), "git-push", "--branch",
                                             "implement/1", "--expected-sha", head])
            self.assertEqual(pushed, 0)
            self.assertEqual(backend.kwargs["controlled"], True)
            push_audit = runtime.core.store.controlled_pushes_for_attempt(result["attempt_id"])
            self.assertEqual(len(push_audit), 1)
            self.assertEqual(push_audit[0]["phase"], "completed")
            marker = Path(workspace) / "temporary-push-change"
            marker.write_text("changed")
            marker.unlink()
            self.assertEqual(runtime.core._workspace_baseline(str(workspace)), attempt["workspace_baseline"])
            self.assertIsNone(attempt["pr_number"])
            completion_result = subprocess.run(
                [str(completion)] if os.name != "nt" else ["cmd", "/c", str(completion)],
                cwd=workspace, env=dict(launches[0]["env"]), capture_output=True, text=True)
            self.assertNotEqual(completion_result.returncode, 0)
            self.assertIn("controlled push", completion_result.stderr.lower())
            self.assertIsNone(runtime.core.store.receipt(result["attempt_id"]))

    def test_exact_head_repair_and_stale_review_launch_counts(self):
        with tempfile.TemporaryDirectory() as temp:
            api = FakeAPI(); writer = FakeWriter(); launches = []
            workspace = init_git_repo(Path(temp) / "repo")
            def runner(args, **kwargs):
                self.assertEqual(args[:3], ["language_server.exe", "agentapi", "new-conversation"])
                launches.append(args[-1]); return SimpleNamespace(returncode=0, stdout=json.dumps({"conversation_id": CONVERSATION_ID}))
            local = {"database_path": str(Path(temp)/"db.sqlite"), "workspace": str(workspace), "owner_id": "owner",
                     "antigravity_executable": "language_server.exe", "app_gh_executable": "app-gh.exe",
                     "app_git_push_executable": "app-git-push.exe", "github_read_token_env": "TOKEN"}
            wired = build_runtime(MANIFEST, local, api, MutatingWriter(api), runner)
            self.assertEqual(wired.core.dispatch_initial("o/r", 1, "owner")["status"], "launched")
            api.issue["labels"] = [{"name": x} for x in ("changes-requested", "frozen-spec")]
            api.canonical_events = [{"willCloseTarget": True,
                                     "source": {"__typename": "PullRequest", "number": 12}}]
            api.pr = {"number": 12, "state": "open", "base": {"ref": "main"}, "user": {"login": "agent"}, "head": {"sha": SHA, "ref": "fix/12"}}
            api.reviews = [{"id": 9, "state": "CHANGES_REQUESTED", "commit_id": SHA, "submitted_at": "now"}]
            candidate = RepairCandidate("o/r", 1, 12, SHA, "fix/12", "reviewer_rejection", "9", "owner")
            self.assertEqual(wired.core.dispatch_repair(candidate)["status"], "launched")
            # The first page contains a canonical link, but another page may
            # contain a second closing PR. Partial results cannot authorize repair.
            api.pr["head"]["sha"] = "b" * 40
            api.reviews = [{"id": 10, "state": "CHANGES_REQUESTED", "commit_id": "b" * 40,
                            "submitted_at": "later"}]
            api.has_next_page = True
            partial_candidate = RepairCandidate("o/r", 1, 12, "b" * 40, "fix/12",
                                                "reviewer_rejection", "10", "owner")
            snapshot = wired.workflow.observe("o/r", 1)
            self.assertFalse(snapshot.pr_open)
            self.assertIsNone(snapshot.pr_number)
            self.assertEqual(wired.core.dispatch_repair(partial_candidate)["status"],
                             "stale_or_ineligible_repair")
            self.assertEqual(len(launches), 2)
            api.has_next_page = False
            api.canonical_events[0]["willCloseTarget"] = False
            no_canonical_link = RepairCandidate("o/r", 1, 12, "b" * 40, "fix/12", "reviewer_rejection", "10", "owner")
            self.assertEqual(wired.core.dispatch_repair(no_canonical_link)["status"], "stale_or_ineligible_repair")
            self.assertEqual(len(launches), 2)
            api.canonical_events[0]["willCloseTarget"] = True
            api.reviews[0]["commit_id"] = "b" * 40
            candidate2 = RepairCandidate("o/r", 1, 12, "c" * 40, "fix/12", "reviewer_rejection", "10", "owner")
            self.assertEqual(wired.core.dispatch_repair(candidate2)["status"], "stale_or_ineligible_repair")
            self.assertEqual(len(launches), 2)

    def test_real_core_exhausted_repair_budget_transitions_through_app_gh(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace = init_git_repo(Path(temp) / "repo")
            api = FakeAPI()
            commands = FakeProductionCommands(api)
            local = {"database_path": str(Path(temp) / "runtime.sqlite"), "workspace": str(workspace),
                     "owner_id": "owner", "antigravity_executable": "language_server.exe",
                     "app_gh_executable": "controlled-app-gh.exe",
                     "app_git_push_executable": "controlled-app-git-push.exe",
                     "github_read_token_env": "TOKEN"}
            runtime = build_runtime(MANIFEST, local, api=api, runner=commands)
            initial = runtime.core.dispatch_initial("o/r", 1, "owner")
            self.assertEqual(initial["status"], "launched")
            api.canonical_events = [{"willCloseTarget": True,
                                     "source": {"__typename": "PullRequest", "number": 12}}]
            api.pr = {"number": 12, "state": "open", "base": {"ref": "main"},
                      "user": {"login": "agent"}, "head": {"sha": "0" * 40, "ref": "fix/12"}}
            for ordinal in range(1, 4):
                head = f"{ordinal:040x}"
                api.pr["head"]["sha"] = head
                candidate = RepairCandidate("o/r", 1, 12, head, "fix/12", "implementation_failure",
                                            f"check-{ordinal}", "owner")
                self.assertEqual(runtime.core.dispatch_repair(candidate)["status"], "launched")
            fourth_head = f"{4:040x}"
            api.pr["head"]["sha"] = fourth_head
            fourth = RepairCandidate("o/r", 1, 12, fourth_head, "fix/12", "implementation_failure",
                                     "check-4", "owner")
            result = runtime.core.dispatch_repair(fourth)
            self.assertTrue(result["needs_human_transitioned"])
            self.assertEqual(runtime.workflow.observe("o/r", 1).coordination_state, "needs-human")
            self.assertEqual(len(commands.agent_launches), 4)
            self.assertIn("issue", commands.app_edits[-1])
            self.assertIn("--remove-label", commands.app_edits[-1])
            self.assertIn("agent-working", commands.app_edits[-1])
            self.assertIn("--add-label", commands.app_edits[-1])
            self.assertIn("needs-human", commands.app_edits[-1])


class TestCredentialProviderConstruction(unittest.TestCase):
    def test_make_credential_provider_supports_posix_and_dpapi_types(self):
        from vccp_runtime.adapters import _make_credential_provider
        from vccp_runtime.github_app import DPAPIFileCredentialSource, POSIXFileCredentialSource
        dpapi_config = {
            "github_app": {
                "app_id": "123456",
                "credential_source": {"type": "windows_dpapi_file", "path": "/path/to/key.dpapi"}
            }
        }
        provider = _make_credential_provider(dpapi_config, "owner/repo")
        self.assertIsInstance(provider.source, DPAPIFileCredentialSource)

        posix_config = {
            "github_app": {
                "app_id": "123456",
                "credential_source": {"type": "posix_file", "path": "/path/to/key.pem"}
            }
        }
        provider = _make_credential_provider(posix_config, "owner/repo")
        self.assertIsInstance(provider.source, POSIXFileCredentialSource)

        invalid_config = {
            "github_app": {
                "app_id": "123456",
                "credential_source": {"type": "invalid_type", "path": "/path/to/key"}
            }
        }
        with self.assertRaisesRegex(ValueError, "credential_source must identify"):
            _make_credential_provider(invalid_config, "owner/repo")


class TestWindowsWrapperRuntimeIsolation(unittest.TestCase):
    def test_windows_wrapper_embeds_exact_runtime_pythonpath(self):
        import sys
        from unittest.mock import patch
        from vccp_runtime.adapters import WorkspaceWriteGuard
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            git_dir = temp_path / ".git"
            git_dir.mkdir()
            hooks_path = [None]
            def fake_runner(args, **kwargs):
                if args[:2] == ["git", "rev-parse"]:
                    return SimpleNamespace(returncode=0, stdout=str(git_dir) + "\n")
                if args[:4] == ["git", "config", "--local", "core.hooksPath"]:
                    hooks_path[0] = args[4]
                    return SimpleNamespace(returncode=0, stdout="")
                if "core.hooksPath" in args:
                    if hooks_path[0]:
                        return SimpleNamespace(returncode=0, stdout=hooks_path[0] + "\n")
                    return SimpleNamespace(returncode=1, stdout="")
                return SimpleNamespace(returncode=0, stdout="")

            guard = WorkspaceWriteGuard(runner=fake_runner)
            launch_context = {
                "python_executable": sys.executable,
                "github_app": {"app_id": "123", "credential_source": {"type": "posix_file", "path": "p"}},
                "repo": "owner/repo",
                "workspace": str(temp_path),
            }
            guard_res = guard.install(str(temp_path), "att-123", provider="antigravity",
                                      launch_context=launch_context, is_windows=True)
            wrapper_cmd = Path(guard_res.git_push_command)
            self.assertTrue(wrapper_cmd.is_file())
            content = wrapper_cmd.read_text(encoding="utf-8")
            self.assertIn("set \"PYTHONPATH=", content)
            self.assertNotIn("PYTHONNOUSERSITE", content)
            self.assertIn("set \"VCCP_CONTROLLED_PUSH=1\"", content)

            context_json = wrapper_cmd.parent / "runtime-context.json"
            self.assertTrue(context_json.is_file())
            context_data = json.loads(context_json.read_text(encoding="utf-8"))
            self.assertIn("runtime_root", context_data)
            expected_root = str(Path(__file__).resolve().parent.parent.parent)
            self.assertEqual(context_data["runtime_root"], expected_root)

    def test_controlled_cli_fails_closed_on_runtime_root_mismatch(self):
        import contextlib
        import io
        with tempfile.TemporaryDirectory() as temp_dir:
            context_path = Path(temp_dir) / "context.json"
            context_path.write_text(json.dumps({
                "github_app": {}, "repo": "owner/repo", "workspace": temp_dir,
                "runtime_root": str(Path(temp_dir) / "wrong-runtime"),
            }), encoding="utf-8")
            with mock.patch.object(controlled_cli, "_provider", side_effect=AssertionError("provider must not load")), \
                    contextlib.redirect_stderr(io.StringIO()):
                result = controlled_cli.run(["--context", str(context_path), "complete-no-change"])
            self.assertEqual(result, 2)


if __name__ == "__main__": unittest.main()
