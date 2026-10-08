import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from vccp_runtime.adapters import (AntiGravityImplementer, GitHubAPI, GitHubAppWriter, GitHubWorkflowAdapter,
                                   WorkspaceWriteGuard, build_runtime, generic_prompt)
from vccp_runtime.core import LaunchDisposition, LaunchRequest, RepairCandidate


SHA = "a" * 40
CONVERSATION_ID = "e13f972a-83b8-4f8a-9a6f-8c7d3b2a1f05"
MANIFEST = {"schema_version": 2, "issue_contract": {"max_automated_repairs": 3,
    "frozen_spec_label": "frozen-spec", "active_coordination_labels": ["agent-ready", "agent-working", "changes-requested"],
    "terminal_coordination_labels": ["infra-blocked", "needs-human"]},
    "repository": {"base_branch": "main", "implementer_authors": ["agent"]}}


class FakeAPI:
    def __init__(self):
        self.issue = {"state": "open", "updated_at": "r1", "labels": [
            {"name": "agent-ready"}, {"name": "frozen-spec"}]}
        self.canonical_events = []
        self.has_next_page = False
        self.pr = None
        self.reviews = []
        self.reads = []

    def get(self, path):
        self.reads.append(path)
        if path.endswith("/reviews?per_page=100"): return self.reviews
        if "/pulls/" in path: return self.pr
        return self.issue

    def graphql(self, query, variables):
        self.query = query
        self.variables = variables
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
        self.assertIsNone(GitHubWorkflowAdapter(api, FakeWriter(), MANIFEST).observe("o/r", 1).formal_review_id)
        api.canonical_events = []
        self.assertIsNone(GitHubWorkflowAdapter(api, FakeWriter(), MANIFEST).observe("o/r", 1).pr_number)

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
                def install(self, *args):
                    result = real_guard.install(*args)
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

    def test_guard_failure_prevents_agentapi_invocation(self):
        calls = []
        class FailedGuard:
            def install(self, *args): raise RuntimeError("cannot install guard")
        adapter = AntiGravityImplementer("language_server.exe", "workspace", "app-gh", "app-git-push",
                                         runner=lambda *a, **kw: calls.append(a), write_guard=FailedGuard())
        result = adapter.launch(LaunchRequest("o/r", 1, "attempt-failed", "initial_dispatch"))
        self.assertEqual(result.disposition, LaunchDisposition.DEFINITELY_NOT_STARTED)
        self.assertEqual(calls, [])

    def test_workspace_guard_blocks_direct_push_without_tracked_changes(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace = init_git_repo(Path(temp) / "repo")
            remote = Path(temp) / "remote.git"
            subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True, text=True)
            subprocess.run(["git", "-C", str(workspace), "remote", "add", "origin", str(remote)],
                           check=True, capture_output=True)
            before = subprocess.run(["git", "status", "--porcelain"], cwd=workspace, capture_output=True, text=True)
            guard = WorkspaceWriteGuard().install(str(workspace), "guarded-attempt", shutil.which("git"))
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
        self.assertIn("C:/tools/app-git-push.exe", prompt)
        for forbidden in ("EasyExam", "Reviewer-only", "three REQUEST_CHANGES rounds"):
            self.assertNotIn(forbidden, prompt)

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
            result = wired.core.dispatch_initial("o/r", 1, "owner")
            self.assertEqual(result["phase"], "LAUNCH_CONFIRMED")
            self.assertEqual(len(launches), 1)
            with self.assertRaises(ValueError): build_runtime({"schema_version": 1}, local, api, FakeWriter(), runner)

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


if __name__ == "__main__": unittest.main()
