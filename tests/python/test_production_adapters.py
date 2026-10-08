import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from vccp_runtime.adapters import (AntiGravityImplementer, GitHubWorkflowAdapter, build_runtime,
                                   generic_prompt)
from vccp_runtime.core import LaunchDisposition, LaunchRequest, RepairCandidate


SHA = "a" * 40
MANIFEST = {"schema_version": 2, "issue_contract": {"max_automated_repairs": 3,
    "frozen_spec_label": "frozen-spec", "active_coordination_labels": ["agent-ready", "agent-working", "changes-requested"],
    "terminal_coordination_labels": ["infra-blocked", "needs-human"]},
    "repository": {"base_branch": "main", "implementer_authors": ["agent"]}}


class FakeAPI:
    def __init__(self):
        self.issue = {"state": "open", "updated_at": "r1", "labels": [
            {"name": "agent-ready"}, {"name": "frozen-spec"}]}
        self.timeline = []
        self.pr = None
        self.reviews = []
        self.reads = []

    def get(self, path):
        self.reads.append(path)
        if "/timeline" in path: return self.timeline
        if path.endswith("/reviews?per_page=100"): return self.reviews
        if "/pulls/" in path: return self.pr
        return self.issue


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
    source = {"number": 12, "state": "open", "pull_request": {"url": "native"}}
    api.timeline = [{"event": "cross-referenced", "source": {"issue": source}}]
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
        api.timeline = []
        self.assertIsNone(GitHubWorkflowAdapter(api, FakeWriter(), MANIFEST).observe("o/r", 1).pr_number)

    def test_ambiguous_wrong_base_and_author_fail_closed(self):
        for mutate in (lambda a: a.timeline.append({"event": "cross-referenced", "source": {"issue": {
                           "number": 13, "state": "open", "pull_request": {"url": "native"}}}}),
                       lambda a: a.pr["base"].update(ref="other"),
                       lambda a: a.pr["user"].update(login="stranger")):
            api = linked_api(); mutate(api)
            snap = GitHubWorkflowAdapter(api, FakeWriter(), MANIFEST).observe("o/r", 1)
            self.assertFalse(snap.pr_open)
            self.assertIsNone(snap.formal_review_id)

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

    def test_antigravity_tristate_and_secret_stripping(self):
        calls, envs = [], []
        def runner(args, **kwargs):
            calls.append(args)
            if args[1] == "prepare": return SimpleNamespace(returncode=0, stdout="")
            envs.append(kwargs["env"])
            return SimpleNamespace(returncode=0, stdout='{"conversation_id":"conv_123"}')
        adapter = AntiGravityImplementer("agent", "workspace", "guard", runner,
                                         environ={"GH_TOKEN": "secret", "KEEP": "yes"})
        req = LaunchRequest("o/r", 1, "attempt", "initial_dispatch")
        result = adapter.launch(req)
        self.assertEqual((result.disposition, result.execution_id), (LaunchDisposition.CONFIRMED, "conv_123"))
        self.assertNotIn("GH_TOKEN", envs[0]); self.assertEqual(envs[0]["KEEP"], "yes")
        for result_value in [SimpleNamespace(returncode=1, stdout=""),
                             SimpleNamespace(returncode=0, stdout="{}"),
                             SimpleNamespace(returncode=0, stdout="not-json")]:
            adapter.runner = lambda args, **kw: SimpleNamespace(returncode=0, stdout="") if args[1] == "prepare" else result_value
            self.assertEqual(adapter.launch(req).disposition, LaunchDisposition.UNKNOWN)
        adapter.runner = lambda *a, **kw: (_ for _ in ()).throw(TimeoutError()) if a[0][1] == "run" else SimpleNamespace(returncode=0, stdout="")
        self.assertEqual(adapter.launch(req).disposition, LaunchDisposition.UNKNOWN)
        adapter.runner = lambda *a, **kw: SimpleNamespace(returncode=1, stdout="")
        self.assertEqual(adapter.launch(req).disposition, LaunchDisposition.DEFINITELY_NOT_STARTED)

    def test_prompt_generic_and_repair_is_exact(self):
        prompt = generic_prompt(LaunchRequest("o/r", 3, "a", "repair", repair_ordinal=2,
            expected_head_sha=SHA, pr_number=12, branch="fix/12"), "local", MANIFEST)
        self.assertIn("existing PR #12", prompt); self.assertIn(SHA, prompt)
        self.assertIn("ordinal 2 is authoritative", prompt)
        self.assertIn("Frozen Issue: #3", prompt)
        for forbidden in ("EasyExam", "Reviewer-only", "three REQUEST_CHANGES rounds"):
            self.assertNotIn(forbidden, prompt)

    def test_runtime_wiring_real_core_initial_and_schema_guard(self):
        with tempfile.TemporaryDirectory() as temp:
            api = FakeAPI(); launches = []
            def runner(args, **kwargs):
                if args[1] == "prepare": return SimpleNamespace(returncode=0, stdout="")
                launches.append(kwargs["input"])
                return SimpleNamespace(returncode=0, stdout='{"id":"exec-1"}')
            local = {"database_path": str(Path(temp)/"db.sqlite"), "workspace": temp, "owner_id": "owner",
                     "antigravity_executable": "agent", "write_guard_executable": "guard",
                     "github_read_token_env": "TOKEN", "github_app_writer_executable": "app-writer"}
            wired = build_runtime(MANIFEST, local, api=api, writer=MutatingWriter(api), runner=runner)
            result = wired.core.dispatch_initial("o/r", 1, "owner")
            self.assertEqual(result["phase"], "LAUNCH_CONFIRMED")
            self.assertEqual(len(launches), 1)
            with self.assertRaises(ValueError): build_runtime({"schema_version": 1}, local, api, FakeWriter(), runner)

    def test_exact_head_repair_and_stale_review_launch_counts(self):
        with tempfile.TemporaryDirectory() as temp:
            api = FakeAPI(); writer = FakeWriter(); launches = []
            def runner(args, **kwargs):
                if args[1] == "prepare": return SimpleNamespace(returncode=0, stdout="")
                launches.append(kwargs["input"]); return SimpleNamespace(returncode=0, stdout='{"id":"exec"}')
            local = {"database_path": str(Path(temp)/"db.sqlite"), "workspace": temp, "owner_id": "owner",
                     "antigravity_executable": "agent", "write_guard_executable": "guard",
                     "github_read_token_env": "TOKEN", "github_app_writer_executable": "app-writer"}
            wired = build_runtime(MANIFEST, local, api, MutatingWriter(api), runner)
            self.assertEqual(wired.core.dispatch_initial("o/r", 1, "owner")["status"], "launched")
            api.issue["labels"] = [{"name": x} for x in ("changes-requested", "frozen-spec")]
            api.timeline = [{"event": "cross-referenced", "source": {"issue": {"number": 12, "state": "open", "pull_request": {"url": "native"}}}}]
            api.pr = {"number": 12, "state": "open", "base": {"ref": "main"}, "user": {"login": "agent"}, "head": {"sha": SHA, "ref": "fix/12"}}
            api.reviews = [{"id": 9, "state": "CHANGES_REQUESTED", "commit_id": SHA, "submitted_at": "now"}]
            candidate = RepairCandidate("o/r", 1, 12, SHA, "fix/12", "reviewer_rejection", "9", "owner")
            self.assertEqual(wired.core.dispatch_repair(candidate)["status"], "launched")
            api.reviews[0]["commit_id"] = "b" * 40
            candidate2 = RepairCandidate("o/r", 1, 12, "c" * 40, "fix/12", "reviewer_rejection", "10", "owner")
            self.assertEqual(wired.core.dispatch_repair(candidate2)["status"], "stale_or_ineligible_repair")
            self.assertEqual(len(launches), 2)


if __name__ == "__main__": unittest.main()
