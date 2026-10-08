import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from vccp_runtime.adapters import (AntiGravityImplementer, GitHubAPI, GitHubAppWriter, GitHubWorkflowAdapter, build_runtime,
                                   generic_prompt)
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
        return {"repository": {"issue": {"timelineItems": {"nodes": self.canonical_events}}}}


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
        self.assertTrue(seen[0].full_url.endswith("/graphql"))
        self.assertEqual(seen[0].get_header("Authorization"), "Bearer read-token")

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

    def test_app_gh_writer_uses_native_issue_edit_arguments(self):
        seen = []
        writer = GitHubAppWriter("C:/controlled/app-gh.exe",
                                 runner=lambda args, **kwargs: (seen.append(args) or SimpleNamespace(returncode=0)))
        self.assertTrue(writer.replace_labels("o/r", 3, ["agent-ready"], ["agent-working"]))
        self.assertEqual(seen[0], ["C:/controlled/app-gh.exe", "issue", "edit", "3", "--repo", "o/r",
                                   "--remove-label", "agent-ready", "--add-label", "agent-working"])

    def test_antigravity_tristate_and_secret_stripping(self):
        calls, envs = [], []
        adapter = AntiGravityImplementer("language_server.exe", "workspace", "app-gh", "app-git-push",
                                         environ={"GH_TOKEN": "secret", "KEEP": "yes"})
        req = LaunchRequest("o/r", 1, "attempt", "initial_dispatch")
        result_value = SimpleNamespace(returncode=0, stdout=json.dumps({"response": {"newConversation": {"conversationId": CONVERSATION_ID}}}))
        def runner(args, **kwargs):
            calls.append(args); envs.append(kwargs["env"]); return result_value
        adapter.runner = runner
        result = adapter.launch(req)
        self.assertEqual((result.disposition, result.execution_id), (LaunchDisposition.CONFIRMED, CONVERSATION_ID))
        self.assertEqual(calls[0][:3], ["language_server.exe", "agentapi", "new-conversation"])
        self.assertIn("Controlled GitHub writer: app-gh", calls[0][-1])
        self.assertIn("Controlled git push wrapper: app-git-push", calls[0][-1])
        self.assertNotIn("GH_TOKEN", envs[0]); self.assertEqual(envs[0]["KEEP"], "yes")
        self.assertEqual(envs[0], {"KEEP": "yes", "VCCP_APP_GH": "app-gh",
                                   "VCCP_APP_GIT_PUSH": "app-git-push"})
        for response in [SimpleNamespace(returncode=1, stdout=json.dumps({"conversationId": CONVERSATION_ID})),
                         SimpleNamespace(returncode=0, stdout="{}"),
                         SimpleNamespace(returncode=0, stdout="not-json"),
                         SimpleNamespace(returncode=0, stdout=json.dumps({"error": "failed", "response": {
                             "newConversation": {"conversationId": CONVERSATION_ID}}})),
                         SimpleNamespace(returncode=0, stdout='{"conversation_id":"conv_123"}'),
                         SimpleNamespace(returncode=0, stdout='{"conversation_id":"exec-1"}'),
                         SimpleNamespace(returncode=0, stdout=json.dumps({"id": CONVERSATION_ID}))]:
            adapter.runner = lambda args, **kw: response
            self.assertEqual(adapter.launch(req).disposition, LaunchDisposition.UNKNOWN)
        adapter.runner = lambda *a, **kw: (_ for _ in ()).throw(TimeoutError())
        self.assertEqual(adapter.launch(req).disposition, LaunchDisposition.UNKNOWN)
        adapter.runner = lambda *a, **kw: (_ for _ in ()).throw(FileNotFoundError())
        self.assertEqual(adapter.launch(req).disposition, LaunchDisposition.DEFINITELY_NOT_STARTED)

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
            def runner(args, **kwargs):
                self.assertEqual(args[:2], ["language_server.exe", "agentapi"])
                launches.append(args[-1])
                return SimpleNamespace(returncode=0, stdout=json.dumps({"conversation_id": CONVERSATION_ID}))
            local = {"database_path": str(Path(temp)/"db.sqlite"), "workspace": temp, "owner_id": "owner",
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
            def runner(args, **kwargs):
                self.assertEqual(args[:3], ["language_server.exe", "agentapi", "new-conversation"])
                launches.append(args[-1]); return SimpleNamespace(returncode=0, stdout=json.dumps({"conversation_id": CONVERSATION_ID}))
            local = {"database_path": str(Path(temp)/"db.sqlite"), "workspace": temp, "owner_id": "owner",
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
            api.canonical_events[0]["willCloseTarget"] = False
            no_canonical_link = RepairCandidate("o/r", 1, 12, "b" * 40, "fix/12", "reviewer_rejection", "10", "owner")
            self.assertEqual(wired.core.dispatch_repair(no_canonical_link)["status"], "stale_or_ineligible_repair")
            self.assertEqual(len(launches), 2)
            api.canonical_events[0]["willCloseTarget"] = True
            api.reviews[0]["commit_id"] = "b" * 40
            candidate2 = RepairCandidate("o/r", 1, 12, "c" * 40, "fix/12", "reviewer_rejection", "10", "owner")
            self.assertEqual(wired.core.dispatch_repair(candidate2)["status"], "stale_or_ineligible_repair")
            self.assertEqual(len(launches), 2)


if __name__ == "__main__": unittest.main()
