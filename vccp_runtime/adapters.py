"""Production adapters for GitHub and AntiGravity; RuntimeCore owns all lifecycle state."""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import subprocess
from typing import Callable
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from .core import (LaunchDisposition, LaunchRequest, LaunchResult, RuntimeCore,
                   WorkflowSnapshot)


class GitHubAPI:
    """Small read transport. The write credential boundary is the separate App writer."""
    def __init__(self, token: str, api_url: str = "https://api.github.com", opener=urlopen):
        if not token:
            raise ValueError("GitHub read token is required")
        self.token, self.api_url, self.opener = token, api_url.rstrip("/"), opener

    def get(self, path: str):
        request = Request(self.api_url + path, headers={
            "Authorization": f"Bearer {self.token}", "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        })
        with self.opener(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))


class GitHubAppWriter:
    """Controlled writer executable receives a single JSON operation on stdin."""
    def __init__(self, executable: str, runner: Callable = subprocess.run):
        if not executable:
            raise ValueError("controlled GitHub App writer executable is required")
        if Path(executable).name.casefold() in {"gh", "gh.exe"}:
            raise ValueError("ordinary gh CLI cannot be used as the production coordination writer")
        self.executable, self.runner = executable, runner

    def replace_labels(self, repo: str, issue: int, remove: list[str], add: list[str]):
        result = self.runner([self.executable, "replace-coordination-labels"],
                             input=json.dumps({"repo": repo, "issue": issue, "remove": remove, "add": add}),
                             text=True, capture_output=True, check=False)
        return result.returncode == 0


class GitHubWorkflowAdapter:
    def __init__(self, api, app_writer, manifest: dict):
        self.api, self.writer, self.manifest = api, app_writer, manifest
        if manifest.get("schema_version") != 2:
            raise ValueError("GitHub adapter requires schema_version 2")
        self.policy = manifest["issue_contract"]
        self.base_branch = manifest["repository"]["base_branch"]
        self.authors = {x.casefold() for x in manifest["repository"]["implementer_authors"]}

    @staticmethod
    def _labels(issue):
        return {x["name"] for x in issue.get("labels", [])}

    def _facts(self, repo, number):
        issue = self.api.get(f"/repos/{repo}/issues/{number}")
        labels = self._labels(issue)
        active = [x for x in self.policy["active_coordination_labels"] if x in labels]
        state = active[0] if len(active) == 1 else "invalid"
        frozen = self.policy["frozen_spec_label"] in labels
        terminal = tuple(x for x in self.policy["terminal_coordination_labels"] if x in labels)
        revision = str(issue.get("updated_at", ""))
        linked = []
        # GitHub timeline cross-references are the canonical native Issue/PR relationship.
        for item in self.api.get(f"/repos/{repo}/issues/{number}/timeline?per_page=100"):
            source = item.get("source", {}).get("issue", {})
            if item.get("event") == "cross-referenced" and source.get("pull_request"):
                prnum = source.get("number")
                if prnum and all(p.get("number") != prnum for p in linked):
                    linked.append(source)
        linked_prs = [self.api.get(f"/repos/{repo}/pulls/{p['number']}") for p in linked]
        open_linked = [p for p in linked_prs if p.get("state") == "open"]
        pr = open_linked[0] if len(open_linked) == 1 else None
        reviews = self.api.get(f"/repos/{repo}/pulls/{pr['number']}/reviews?per_page=100") if pr else None
        head = pr.get("head", {}) if pr else {}
        current_sha = head.get("sha")
        current_review = None
        if pr and len(open_linked) == 1 and pr.get("base", {}).get("ref") == self.base_branch and \
                pr.get("user", {}).get("login", "").casefold() in self.authors:
            current_head_reviews = [r for r in reviews or []
                                    if r.get("commit_id", "").casefold() == (current_sha or "").casefold()]
            if current_head_reviews:
                latest = max(current_head_reviews, key=lambda r: r.get("submitted_at", ""))
                if latest.get("state") == "CHANGES_REQUESTED":
                    current_review = latest
        valid_pr = bool(pr and len(open_linked) == 1
                        and pr.get("base", {}).get("ref") == self.base_branch
                        and pr.get("user", {}).get("login", "").casefold() in self.authors)
        return WorkflowSnapshot(
            repo=repo, issue_number=number, revision=revision, issue_open=issue.get("state") == "open",
            coordination_state=state, frozen_spec=frozen, terminal_labels=terminal,
            canceled=bool(issue.get("locked") or labels & {"cancelled", "canceled", "authorization-lost"}),
            open_linked_pr_count=len(open_linked), pr_number=pr.get("number") if valid_pr else None,
            pr_open=bool(valid_pr and pr.get("state") == "open"),
            pr_linked_issue=number if valid_pr else None, pr_head_sha=current_sha if valid_pr else None,
            pr_branch=head.get("ref") if valid_pr else None,
            formal_review_state=current_review.get("state") if current_review else None,
            formal_review_id=str(current_review.get("id")) if current_review else None,
            formal_review_head_sha=current_review.get("commit_id") if current_review else None,
        )

    def observe(self, repo, issue_number):
        return self._facts(repo, issue_number)

    def transition_coordination_state(self, repo, issue_number, expected_state, new_state, expected_revision):
        before = self._facts(repo, issue_number)
        if before.revision != str(expected_revision) or before.coordination_state != expected_state or \
                before.terminal_labels or before.canceled or not before.issue_open:
            return False
        if expected_state not in self.policy["active_coordination_labels"] or \
                new_state not in self.policy["active_coordination_labels"]:
            return False
        remove = [x for x in self.policy["active_coordination_labels"] if x != new_state and x == expected_state]
        if not self.writer.replace_labels(repo, issue_number, remove, [new_state]):
            return False
        after = self._facts(repo, issue_number)
        return (after.issue_open and not after.terminal_labels and not after.canceled
                and after.coordination_state == new_state)


def generic_prompt(request: LaunchRequest, workspace: str, policy: dict) -> str:
    common = (f"Repository: {request.repo}\nFrozen Issue: #{request.issue_number}\nWorkspace: {workspace}\n"
              "Read the Frozen Issue from GitHub and treat it as the sole task specification. "
              "Do not expand scope or merge. Report tests actually run. Use configured controlled GitHub App "
              "wrappers for GitHub writes and git push; never use host-human credentials. Do not persist or "
              "request GitHub tokens.\n")
    if request.attempt_kind == "repair":
        return common + (f"Repair the existing PR #{request.pr_number} on existing branch {request.branch}. "
                         f"The exact supplied baseline is {request.expected_head_sha}; repair ordinal "
                         f"{request.repair_ordinal} is authoritative. Do not create another PR/branch, "
                         "count reviews, or calculate repair budget.\n")
    return common + (f"Implement on a non-base branch and open one PR linked to this Frozen Issue, "
                     f"using base branch {policy['repository']['base_branch']}.\n")


class AntiGravityImplementer:
    def __init__(self, executable: str, workspace: str, write_guard: str,
                 runner: Callable = subprocess.run, timeout: int = 60, environ=None):
        self.executable, self.workspace, self.write_guard = executable, workspace, write_guard
        self.runner, self.timeout = runner, timeout
        self.environ = os.environ if environ is None else environ
        if not executable or not workspace or not write_guard:
            raise ValueError("AntiGravity executable, workspace, and write guard are required")

    def launch(self, request):
        # The guard validates first, then owns the child process and its Git/GitHub credential boundary.
        try:
            guarded = self.runner([self.write_guard, "prepare", self.workspace], text=True,
                                  capture_output=True, check=False, timeout=self.timeout)
        except Exception:
            return LaunchResult(LaunchDisposition.DEFINITELY_NOT_STARTED)
        if guarded.returncode != 0:
            return LaunchResult(LaunchDisposition.DEFINITELY_NOT_STARTED)
        env = dict(self.environ)
        for key in list(env):
            if key.upper() in {"GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_APP_PRIVATE_KEY"}:
                env.pop(key)
        prompt = generic_prompt(request, self.workspace, self._policy)
        try:
            result = self.runner([self.write_guard, "run", self.workspace, self.executable, "launch", "--json"], env=env,
                                 input=prompt, text=True, capture_output=True, check=False,
                                 timeout=self.timeout)
        except Exception:
            return LaunchResult(LaunchDisposition.UNKNOWN)
        try:
            payload = json.loads(result.stdout)
            for _ in range(2):
                if isinstance(payload, str):
                    payload = json.loads(payload)
            identity = payload.get("conversation_id") or payload.get("conversationId") or payload.get("id")
            if result.returncode == 0 and isinstance(identity, str) and identity.strip() and \
                    len(identity.strip()) <= 200 and all(c.isalnum() or c in "_-" for c in identity.strip()):
                return LaunchResult(LaunchDisposition.CONFIRMED, identity.strip())
        except Exception:
            pass
        return LaunchResult(LaunchDisposition.UNKNOWN)

    _policy = {"repository": {"base_branch": ""}}


@dataclass(frozen=True)
class RuntimeAdapters:
    core: RuntimeCore
    workflow: GitHubWorkflowAdapter
    implementer: AntiGravityImplementer


def build_runtime(manifest: dict, local_config: dict, api=None, writer=None, runner=subprocess.run):
    """Construct the existing core; local paths and executable settings stay machine-local."""
    if manifest.get("schema_version") != 2:
        raise ValueError("runtime wiring requires schema_version 2")
    required = ("database_path", "workspace", "owner_id", "antigravity_executable",
                "write_guard_executable", "github_read_token_env", "github_app_writer_executable")
    if any(not local_config.get(k) for k in required):
        raise ValueError("incomplete machine-local runtime configuration")
    api = api or GitHubAPI(os.environ.get(local_config["github_read_token_env"], ""))
    writer = writer or GitHubAppWriter(local_config["github_app_writer_executable"], runner)
    workflow = GitHubWorkflowAdapter(api, writer, manifest)
    implementer = AntiGravityImplementer(local_config["antigravity_executable"], local_config["workspace"],
                                        local_config["write_guard_executable"], runner,
                                        local_config.get("launch_timeout_seconds", 60))
    implementer._policy = manifest
    core = RuntimeCore(local_config["database_path"], manifest, workflow, implementer)
    return RuntimeAdapters(core, workflow, implementer)
