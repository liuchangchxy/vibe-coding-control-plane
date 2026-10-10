import json
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

from vccp_runtime.adapters import build_runtime
from vccp_runtime.orchestrator import run_once


SHA_A = "a" * 40
SHA_B = "b" * 40
UUID = "e13f972a-83b8-4f8a-9a6f-8c7d3b2a1f05"


def manifest(base, author, check, implementation, infrastructure):
    return {
        "schema_version": 2,
        "issue_contract": {
            "max_automated_repairs": 3,
            "frozen_spec_label": "frozen-spec",
            "active_coordination_labels": ["agent-ready", "agent-working", "changes-requested"],
            "terminal_coordination_labels": ["infra-blocked", "needs-human"],
        },
        "repository": {"base_branch": base, "implementer_authors": [author]},
        "reviewer": {"required_checks": [{
            "name": check, "accepted_conclusions": ["success"],
            "implementation_failure_conclusions": implementation,
            "infrastructure_failure_conclusions": infrastructure,
        }]},
    }


class FakeGitHub:
    def __init__(self):
        self.issues = {}
        self.prs = {}
        self.events = {}
        self.reviews = {}
        self.checks = {}
        self.merge_calls = []
        self.reads = []

    def add_consumer(self, repo, base, author, issue):
        key = (repo, issue)
        self.issues[key] = {"number": issue, "state": "open", "updated_at": "r1",
                            "labels": [{"name": "agent-ready"}, {"name": "frozen-spec"}]}
        self.prs[key] = None
        self.events[key] = []
        self.reviews[key] = []
        self.checks[key] = {}
        return key

    def link_pr(self, key, number, base, author, branch, sha):
        self.events[key] = [{"willCloseTarget": True,
                             "source": {"__typename": "PullRequest", "number": number}}]
        self.prs[key] = {"number": number, "state": "open", "merged": False,
                         "base": {"ref": base}, "user": {"login": author},
                         "head": {"sha": sha, "ref": branch}}

    def get(self, path):
        self.reads.append(path)
        if "/issues?" in path:
            repo = path.split("/repos/", 1)[1].split("/issues?", 1)[0]
            label = path.split("labels=", 1)[1].split("&", 1)[0]
            return [issue.copy() for (key_repo, _), issue in self.issues.items()
                    if key_repo == repo and issue["state"] == "open"
                    and any(x["name"] == label for x in issue["labels"])]
        repo = path.split("/repos/", 1)[1].split("/issues/", 1)[0].split("/pulls/", 1)[0]
        repo = repo.split("/commits/", 1)[0]
        key = next((item for item in self.issues if item[0] == repo), None)
        if key is None:
            raise AssertionError(f"unknown fake repository in {path}")
        issue = self.issues[key]
        if "/issues/" in path and "?" not in path:
            return issue
        if path.endswith("/reviews?per_page=100"):
            return self.reviews[key]
        if "/check-runs?per_page=100" in path:
            sha = path.split("/commits/", 1)[1].split("/check-runs", 1)[0]
            runs = self.checks[key].get(sha, [])
            return {"total_count": len(runs), "check_runs": runs}
        if "/pulls/" in path:
            return self.prs[key]
        raise AssertionError(f"unexpected GitHub GET {path}")

    def graphql(self, _query, variables):
        key = (f"{variables['owner']}/{variables['name']}", variables["number"])
        return {"repository": {"issue": {"timelineItems": {
            "nodes": self.events[key], "pageInfo": {"hasNextPage": False}}}}}


class FakeAppWriter:
    def __init__(self, github):
        self.github = github
        self.calls = []

    def replace_labels(self, repo, number, remove, add):
        self.calls.append((repo, number, list(remove), list(add)))
        issue = self.github.issues[(repo, number)]
        issue["labels"] = [item for item in issue["labels"] if item["name"] not in remove]
        existing = {item["name"] for item in issue["labels"]}
        issue["labels"].extend({"name": label} for label in add if label not in existing)
        issue["updated_at"] += "+"
        return True


class FakeAgentAPI:
    def __init__(self):
        self.launches = []

    def __call__(self, args, **kwargs):
        if args[0] != "language_server.exe":
            raise AssertionError(f"unexpected external command {args[0]}")
        self.launches.append(list(args))
        return SimpleNamespace(returncode=0, stdout=json.dumps({"conversation_id": UUID}), stderr="")


def git_repo(path):
    path.mkdir()
    subprocess.run(["git", "init", str(path)], check=True, capture_output=True, text=True)
    subprocess.run(["git", "-C", str(path), "-c", "user.name=Acceptance", "-c",
                    "user.email=acceptance@example.invalid", "commit", "--allow-empty",
                    "-m", "baseline"], check=True, capture_output=True, text=True)


class FinalIntegrationAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = self.root / "shared.sqlite"
        self.github = FakeGitHub()
        self.writer = FakeAppWriter(self.github)
        self.agent = FakeAgentAPI()
        self.a = ("consumer-a/project", 7)
        self.b = ("consumer-b/service", 7)
        self.github.add_consumer(*self.a[:1], "trunk", "a-builder[bot]", self.a[1])
        self.github.add_consumer(*self.b[:1], "develop", "b-implementer[bot]", self.b[1])
        self.manifests = {
            self.a[0]: manifest("trunk", "a-builder[bot]", "A / Verify", ["failure"], ["timed_out"]),
            self.b[0]: manifest("develop", "b-implementer[bot]", "B / Unit", ["action_required"], ["startup_failure"]),
        }
        self.workspaces = {}
        for index, repo in enumerate(self.manifests):
            workspace = self.root / f"workspace-{index}"
            git_repo(workspace)
            self.workspaces[repo] = workspace

    def tearDown(self):
        self.temp.cleanup()

    def runtime(self, repo):
        local = {"database_path": str(self.db), "workspace": str(self.workspaces[repo]),
                 "owner_id": "acceptance-owner", "antigravity_executable": "language_server.exe",
                 "app_gh_executable": "controlled-app-gh.exe",
                 "app_git_push_executable": "controlled-app-git-push.exe",
                 "github_read_token_env": "UNUSED_FAKE_TOKEN",
                 "recovery_timeout_seconds": 900,
                 "lifecycle_timeouts": {"waiting_ci": 1000, "waiting_review": 1000, "waiting_merge": 1000}}
        return build_runtime(self.manifests[repo], local, api=self.github, writer=self.writer, runner=self.agent)

    def accepted(self, name, sha, id=1):
        return {"name": name, "head_sha": sha, "status": "completed", "conclusion": "success", "id": id}

    def test_two_consumers_complete_happy_and_repair_flows_with_restart_isolation(self):
        # Consumer A: production discovery dispatches eligible work without an Issue argument.
        runtime_a = self.runtime(self.a[0])
        first = run_once(runtime_a, self.a[0], "acceptance-owner", now=100)
        self.assertEqual(first["dispatches"][0]["status"], "launched")
        self.assertTrue(any("labels=agent-ready" in path for path in self.github.reads))
        self.assertEqual(runtime_a.core.store.attempts_for_repo(self.a[0])[0]["phase"], "LAUNCH_CONFIRMED")
        self.assertEqual(len(self.agent.launches), 1)
        duplicate_cycle = run_once(runtime_a, self.a[0], "acceptance-owner", now=100.5)
        self.assertEqual(duplicate_cycle["dispatches"], [])
        self.assertEqual(len(self.agent.launches), 1)
        self.github.link_pr(self.a, 41, "trunk", "a-builder[bot]", "work/a-7", SHA_A)
        result = run_once(runtime_a, self.a[0], "acceptance-owner", now=102)
        self.assertEqual(result["lifecycle"]["items"][0]["status"], "WAITING_CI")
        # Reconstruct runtime against the same DB while A is durably waiting on exact-head CI.
        ci_deadline = runtime_a.core.store.lifecycle_state(*self.a)["deadline_at"]
        runtime_a = self.runtime(self.a[0])
        result = run_once(runtime_a, self.a[0], "acceptance-owner", now=103)
        self.assertEqual(runtime_a.core.store.lifecycle_state(*self.a)["deadline_at"], ci_deadline)
        self.assertEqual(len(self.agent.launches), 1)
        self.github.checks[self.a][SHA_A] = [self.accepted("A / Verify", SHA_A)]
        self.github.reviews[self.a] = [{"id": 1, "state": "APPROVED", "commit_id": SHA_A,
                                       "submitted_at": "2026-10-08T00:00:00Z"}]
        result = run_once(runtime_a, self.a[0], "acceptance-owner", now=104)
        self.assertEqual(result["lifecycle"]["items"][0]["status"], "WAITING_MERGE")
        self.github.prs[self.a].update(state="closed", merged=True)
        self.github.issues[self.a]["state"] = "closed"  # GitHub closes a linked Issue on merge.
        result = run_once(runtime_a, self.a[0], "acceptance-owner", now=105)
        self.assertEqual(result["lifecycle"]["items"][0]["status"], "merged_success")
        self.assertEqual(self.github.merge_calls, [])
        self.assertFalse(hasattr(runtime_a.workflow, "merge"))
        labels_a = {item["name"] for item in self.github.issues[self.a]["labels"]}
        self.assertFalse(labels_a & {"agent-ready", "agent-working", "changes-requested"})
        with closing(sqlite3.connect(self.db)) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM leases WHERE repo=? AND issue_number=?",
                                                (self.a[0], self.a[1])).fetchone()[0], 0)
        self.assertFalse(runtime_a.core.store.flow(*self.a)["trusted"])

        # Consumer B: its distinct manifest routes an attributed failure through dispatch_repair.
        runtime_b = self.runtime(self.b[0])
        result = run_once(runtime_b, self.b[0], "acceptance-owner", self.b[1], now=200)
        self.assertEqual(result["dispatch"]["status"], "launched")
        self.github.link_pr(self.b, 9, "develop", "b-implementer[bot]", "patch/b-7", SHA_A)
        run_once(runtime_b, self.b[0], "acceptance-owner", now=201)
        self.github.checks[self.b][SHA_A] = [{"name": "B / Unit", "head_sha": SHA_A,
            "status": "completed", "conclusion": "action_required", "id": 2}]
        failed = run_once(runtime_b, self.b[0], "acceptance-owner", now=202)
        self.assertEqual(failed["lifecycle"]["items"][0]["trigger"], "ci_repair")
        self.assertEqual(failed["lifecycle"]["items"][0]["status"], "launched")
        self.assertEqual(len(self.agent.launches), 3)
        repair_attempt = [row for row in runtime_b.core.store.attempts_for_repo(self.b[0])
                          if row["kind"] == "repair"][0]
        self.assertEqual((repair_attempt["pr_number"], repair_attempt["branch"],
                          repair_attempt["expected_head_sha"]), (9, "patch/b-7", SHA_A))
        # Persist and reload while repair is in flight; old head cannot restart lifecycle or launch.
        old_state = runtime_b.core.store.lifecycle_state(*self.b)
        runtime_b = self.runtime(self.b[0])
        waiting = run_once(runtime_b, self.b[0], "acceptance-owner", now=203)
        self.assertEqual(waiting["lifecycle"]["items"][0]["status"], "waiting_for_repair")
        self.assertEqual(runtime_b.core.store.lifecycle_state(*self.b)["deadline_at"], old_state["deadline_at"])
        self.assertEqual(len(self.agent.launches), 3)
        self.assertEqual(self.github.prs[self.b]["number"], 9)
        self.assertEqual(self.github.prs[self.b]["head"]["ref"], "patch/b-7")

        # D1 binds the pushed exact new head on the existing PR and branch.
        self.github.prs[self.b]["head"]["sha"] = SHA_B
        self.github.checks[self.b][SHA_B] = []
        bound = run_once(runtime_b, self.b[0], "acceptance-owner", now=204)
        self.assertEqual(bound["lifecycle"]["items"][0]["status"], "WAITING_CI")
        self.assertEqual(runtime_b.core.store.lifecycle_state(*self.b)["head_sha"], SHA_B)
        self.github.checks[self.b][SHA_B] = [self.accepted("B / Unit", SHA_B, 3)]
        # Positive review on the old head must not authorize the new head.
        self.github.reviews[self.b] = [{"id": 3, "state": "APPROVED", "commit_id": SHA_A,
                                        "submitted_at": "2026-10-08T00:00:00Z"}]
        stale = run_once(runtime_b, self.b[0], "acceptance-owner", now=205)
        self.assertEqual(stale["lifecycle"]["items"][0]["status"], "WAITING_REVIEW")
        self.github.reviews[self.b].append({"id": 4, "state": "APPROVED", "commit_id": SHA_B,
                                            "submitted_at": "2026-10-08T00:01:00Z"})
        self.assertEqual(run_once(runtime_b, self.b[0], "acceptance-owner", now=206)["lifecycle"]
                         ["items"][0]["status"], "WAITING_MERGE")
        self.github.prs[self.b].update(state="closed", merged=True)
        self.github.issues[self.b]["state"] = "closed"
        self.assertEqual(run_once(runtime_b, self.b[0], "acceptance-owner", now=207)["lifecycle"]
                         ["items"][0]["status"], "merged_success")

        # Shared SQLite is partitioned by repository identity; only B owns a repair.
        self.assertEqual(len([row for row in runtime_a.core.store.attempts_for_repo(self.a[0])
                              if row["kind"] == "repair"]), 0)
        b_repairs = [row for row in runtime_b.core.store.attempts_for_repo(self.b[0])
                     if row["kind"] == "repair"]
        self.assertEqual(len(b_repairs), 1)
        self.assertEqual(b_repairs[0]["phase"], "MERGED_SUCCESS")
        self.assertEqual((self.manifests[self.a[0]]["repository"]["base_branch"],
                          self.manifests[self.b[0]]["repository"]["base_branch"]), ("trunk", "develop"))
        self.assertNotEqual(self.manifests[self.a[0]]["reviewer"]["required_checks"][0]["name"],
                            self.manifests[self.b[0]]["reviewer"]["required_checks"][0]["name"])

    def test_first_reviewer_rejection_dispatches_repair_ordinal_one_without_duplicate(self):
        runtime_a = self.runtime(self.a[0])
        # 1. Initial dispatch
        result = run_once(runtime_a, self.a[0], "acceptance-owner", self.a[1], now=100)
        self.assertEqual(result["dispatch"]["status"], "launched")
        self.assertEqual(len(self.agent.launches), 1)

        # 2. PR bound and passes CI check
        self.github.link_pr(self.a, 11, "trunk", "a-builder[bot]", "patch/a-7", SHA_A)
        self.github.checks[self.a][SHA_A] = [self.accepted("A / Verify", SHA_A, 1)]
        bound = run_once(runtime_a, self.a[0], "acceptance-owner", now=101)
        self.assertEqual(bound["lifecycle"]["items"][0]["status"], "WAITING_REVIEW")

        # 3. Reviewer submits CHANGES_REQUESTED
        self.github.issues[self.a]["labels"] = [{"name": "changes-requested"}, {"name": "frozen-spec"}]
        self.github.reviews[self.a] = [{"id": 10, "state": "CHANGES_REQUESTED", "commit_id": SHA_A,
                                        "submitted_at": "2026-10-08T00:00:00Z"}]

        # 4. Orchestrator cycle: advance_once dispatches reviewer repair (ordinal=1), reconcile_once does NOT mark needs-human
        cycle = run_once(runtime_a, self.a[0], "acceptance-owner", now=102)
        self.assertEqual(cycle["lifecycle"]["items"][0]["trigger"], "reviewer_repair")
        self.assertEqual(cycle["lifecycle"]["items"][0]["status"], "launched")
        self.assertEqual(cycle["lifecycle"]["items"][0]["ordinal"], 1)
        self.assertEqual(len(self.agent.launches), 2)
        labels = {item["name"] for item in self.github.issues[self.a]["labels"]}
        self.assertIn("agent-working", labels)
        self.assertNotIn("changes-requested", labels)
        self.assertIn("frozen-spec", labels)

        # Verify attempts state in DB
        repairs = [row for row in runtime_a.core.store.attempts_for_repo(self.a[0]) if row["kind"] == "repair"]
        self.assertEqual(len(repairs), 1)
        self.assertEqual(repairs[0]["repair_ordinal"], 1)
        self.assertEqual(repairs[0]["cause_type"], "reviewer_rejection")

        # 5. Subsequent cycle while repair is in-flight: waiting_for_repair, no duplicate dispatch
        cycle2 = run_once(runtime_a, self.a[0], "acceptance-owner", now=103)
        self.assertEqual(cycle2["lifecycle"]["items"][0]["status"], "waiting_for_repair")
        self.assertEqual(len(self.agent.launches), 2)


if __name__ == "__main__":
    unittest.main()
