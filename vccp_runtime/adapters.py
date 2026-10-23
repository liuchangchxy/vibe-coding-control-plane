"""Production adapters for GitHub and AntiGravity; RuntimeCore owns all lifecycle state."""
from __future__ import annotations

from dataclasses import dataclass
import json
import hashlib
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import time
from typing import Callable
from urllib.parse import quote, unquote, urlparse
from urllib.request import Request, urlopen

from .core import (LaunchDisposition, LaunchRequest, LaunchResult, RuntimeCore,
                   WorkflowSnapshot)
from .github_app import (DPAPIFileCredentialSource, GitHubAppCredentialProvider,
                         GitHubAppError, GitHubAppIdentityError, GitHubAppPermissionError,
                         GitHubHTTPError, POSIXFileCredentialSource, make_credential_source,
                         repository_from_remote)
from .github_push import GitHubAppPushBackend
from .providers import (ANTIGRAVITY_PROVIDER, CLAUDE_AGENT_SDK_PROVIDER, CLAUDE_CODE_PROVIDER,
                        ProviderRouter, normalize_provider_key)


_CONVERSATION_UUID = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)

# Host GitHub credentials are never inherited by a launched implementer; it may only
# reach GitHub through the controlled App writer and controlled push wrapper.
_GITHUB_CREDENTIAL_ENVIRONMENT = frozenset({
    "GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_APP_TOKEN", "GITHUB_APP_PRIVATE_KEY",
})

# A Claude implementer is launched from the VCCP runner, but the runner itself may be
# started inside another Claude Code session; that session's identity must not leak.
_CLAUDE_SESSION_ENVIRONMENT = frozenset({
    "CLAUDECODE", "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_CHILD_SESSION", "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_PID", "CLAUDE_CODE_MESSAGING_SOCKET", "CLAUDE_CODE_MESSAGING_TOKEN",
})

# `claude --bg` otherwise fences the session out of the shared checkout through a
# git-worktree isolation guard, which VCCP's in-workspace branch/PR model cannot use.
_CLAUDE_BACKGROUND_SETTINGS = json.dumps({"worktree": {"bgIsolation": "none"}})
_CLAUDE_BACKGROUND_HANDLE = re.compile(r"backgrounded\s+\S*\s*([0-9a-f]{8})\b")


class GitHubAPI:
    """GitHub REST/GraphQL reads using a short-lived App token in production."""
    def __init__(self, token: str | None = None, api_url: str = "https://api.github.com",
                 graphql_url: str | None = None, opener=urlopen):
        if not token:
            raise ValueError("legacy test token or a credential provider is required")
        if api_url.rstrip("/") != "https://api.github.com" and not graphql_url:
            raise ValueError("custom GitHub REST API URLs require an explicit GraphQL endpoint")
        self.token, self.api_url, self.opener = token, api_url.rstrip("/"), opener
        self.graphql_url = graphql_url or "https://api.github.com/graphql"

    @classmethod
    def with_app_provider(cls, provider: GitHubAppCredentialProvider,
                          graphql_url: str | None = None):
        instance = cls.__new__(cls)
        instance.token = None
        instance.provider = provider
        instance.api_url = provider.api_url
        instance.opener = None
        instance.graphql_url = graphql_url or provider.api_url.replace("/api/v3", "") + "/graphql"
        return instance

    @staticmethod
    def _repo_from_path(path: str) -> str:
        match = re.match(r"^/repos/([^/]+/[^/?]+)(?:/|\?|$)", path)
        if not match:
            raise ValueError("GitHub API read must identify a repository")
        return match.group(1)

    def get(self, path: str):
        if getattr(self, "provider", None) is not None:
            repo = self._repo_from_path(path)
            status, payload = self.provider.request("GET", path, repo)
            if status < 200 or status >= 300:
                raise GitHubHTTPError(status, "GET", path)
            return payload
        request = Request(self.api_url + path, headers={
            "Authorization": f"Bearer {self.token}", "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        })
        with self.opener(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))

    def graphql(self, query: str, variables: dict):
        if getattr(self, "provider", None) is not None:
            repo = f"{variables['owner']}/{variables['name']}"
            status, payload = self.provider.request("POST", "/graphql", repo,
                                                    {"query": query, "variables": variables})
            if status < 200 or status >= 300 or payload.get("errors"):
                raise GitHubHTTPError(status, "GraphQL")
            return payload["data"]
        request = Request(self.graphql_url,
                          data=json.dumps({"query": query, "variables": variables}).encode("utf-8"),
                          headers={"Authorization": f"Bearer {self.token}",
                                   "Accept": "application/vnd.github+json",
                                   "Content-Type": "application/json",
                                   "X-GitHub-Api-Version": "2022-11-28"}, method="POST")
        with self.opener(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if payload.get("errors"):
            raise RuntimeError("GitHub GraphQL relationship read failed")
        return payload["data"]


class GitHubAppWriter:
    """Bounded GitHub App REST writer; the executable path is retained for test injection only."""
    def __init__(self, executable: str | None = None, runner: Callable = subprocess.run,
                 *, credential_provider: GitHubAppCredentialProvider | None = None,
                 target_repository: str | None = None, allowed_labels=None):
        if credential_provider is not None:
            if not target_repository:
                raise ValueError("controlled writer target repository is required")
            self.provider = credential_provider
            self.target_repository = target_repository.casefold()
            self.allowed_labels = set(allowed_labels or ())
            if not self.allowed_labels:
                raise ValueError("controlled writer must be fenced to coordination labels")
            self.executable = None
        else:
            # Backward-compatible command seam for deterministic adapter tests.
            if not executable or Path(executable).name.casefold() in {"gh", "gh.exe"}:
                raise ValueError("production coordination writer must use a GitHub App provider")
            self.provider = None
            self.executable, self.runner = executable, runner

    def replace_labels(self, repo: str, issue: int, remove: list[str], add: list[str]):
        if self.provider is not None:
            if repo.casefold() != self.target_repository or not isinstance(issue, int) or issue < 1:
                raise GitHubAppIdentityError("controlled writer target does not match enrolled repository")
            if (len(set(remove)) != len(remove) or len(set(add)) != len(add)
                    or set(remove) & set(add)
                    or any(label not in self.allowed_labels for label in (*remove, *add))):
                raise GitHubAppIdentityError("controlled writer operation is outside coordination-label policy")
            owner, name = repo.split("/", 1)
            path = f"/repos/{owner}/{name}/issues/{issue}"
            try:
                status, current = self.provider.request("GET", path, repo)
                if status != 200 or current.get("number") != issue:
                    return False
                repo_url = str(current.get("repository_url", "")).rstrip("/").casefold()
                expected_url = f"{self.provider.api_url}/repos/{owner}/{name}".casefold()
                if repo_url and repo_url != expected_url:
                    return False
                before = {item.get("name") for item in current.get("labels", []) if isinstance(item, dict)}
                if not set(remove).issubset(before):
                    return False
                for label in remove:
                    code, _ = self.provider.request("DELETE", f"{path}/labels/{quote(label, safe='')}", repo)
                    if code not in (200, 204, 404):
                        return False
                if add:
                    code, _ = self.provider.request("POST", f"{path}/labels", repo, {"labels": add})
                    if code not in (200, 201):
                        return False
                verified_status, verified = self.provider.request("GET", path, repo)
                if verified_status != 200 or verified.get("number") != issue:
                    return False
                after = {item.get("name") for item in verified.get("labels", []) if isinstance(item, dict)}
                return after == (before - set(remove)) | set(add)
            except GitHubAppError:
                return False
        command = [self.executable, "issue", "edit", str(issue), "--repo", repo]
        for label in remove:
            command.extend(("--remove-label", label))
        for label in add:
            command.extend(("--add-label", label))
        result = self.runner(command, text=True, capture_output=True, check=False)
        return result.returncode == 0

    def create_pull_request(self, repo: str, issue: int, title: str, body: str,
                            head: str, base: str, draft: bool = False):
        if self.provider is None or repo.casefold() != self.target_repository:
            raise GitHubAppIdentityError("controlled PR creation requires the enrolled GitHub App provider")
        if not isinstance(issue, int) or issue < 1 or not all(isinstance(x, str) and x.strip()
                                                            for x in (title, body, head, base)):
            raise ValueError("controlled PR creation arguments are invalid")
        closing_reference = (rf"(?<![A-Za-z0-9_])(?:close(?:s|d)?|fix(?:es|ed)?|"
                             rf"resolve(?:s|d)?)(?:\s*:\s*|\s+)#{issue}(?![A-Za-z0-9])")
        if not re.search(closing_reference, body, re.IGNORECASE):
            raise GitHubAppIdentityError(
                "controlled PR body must close its Frozen Issue with a GitHub closing keyword"
            )
        if ":" in head or head.startswith("-") or head == base:
            raise GitHubAppIdentityError("controlled PR head or base is outside the same-repository policy")
        owner, name = repo.split("/", 1)
        status, result = self.provider.request("POST", f"/repos/{owner}/{name}/pulls", repo,
                                              {"title": title, "body": body, "head": head,
                                               "base": base, "draft": bool(draft)})
        if status != 201 or not isinstance(result.get("number"), int):
            raise GitHubAppError("controlled pull request creation failed")
        return result["number"]

    def close_issue(self, repo: str, issue: int):
        if self.provider is None or repo.casefold() != self.target_repository:
            raise GitHubAppIdentityError("controlled Issue close requires the enrolled GitHub App provider")
        if not isinstance(issue, int) or issue < 1:
            raise ValueError("controlled Issue identity is invalid")
        owner, name = repo.split("/", 1)
        path = f"/repos/{owner}/{name}/issues/{issue}"
        status, current = self.provider.request("GET", path, repo)
        if status != 200 or current.get("number") != issue or current.get("state") != "open":
            return current.get("state") == "closed" if isinstance(current, dict) else False
        code, _ = self.provider.request("PATCH", path, repo, {"state": "closed", "state_reason": "completed"})
        if code not in (200, 201):
            return False
        verified_status, verified = self.provider.request("GET", path, repo)
        return verified_status == 200 and verified.get("number") == issue and verified.get("state") == "closed"


class GitHubWorkflowAdapter:
    _CANONICAL_LINKS_QUERY = """
    query CanonicalClosingPullRequests($owner: String!, $name: String!, $number: Int!) {
      repository(owner: $owner, name: $name) {
        issue(number: $number) {
          timelineItems(first: 100, itemTypes: [CROSS_REFERENCED_EVENT]) {
            pageInfo { hasNextPage }
            nodes {
              ... on CrossReferencedEvent {
                willCloseTarget
                source { __typename ... on PullRequest { number } }
              }
            }
          }
        }
      }
    }
    """

    _MERGED_CLOSING_PR_QUERY = """
    query MergedClosingPullRequest($owner: String!, $name: String!, $number: Int!) {
      repository(owner: $owner, name: $name) {
        pullRequest(number: $number) {
          number
          state
          merged
          baseRefName
          headRefName
          headRefOid
          closingIssuesReferences(first: 10) {
            nodes { number }
          }
        }
      }
    }
    """

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
        terminal = tuple(x for x in self.policy["terminal_coordination_labels"] if x in labels)
        if len(active) == 1 and not terminal:
            state = active[0]
        elif not active and len(terminal) == 1:
            state = terminal[0]
        else:
            state = "invalid"
        frozen = self.policy["frozen_spec_label"] in labels
        body = issue.get("body") or ""
        marker_lines = [line.strip() for line in body.splitlines()
                        if line.strip().startswith("completion_policy:")]
        allow_no_change = marker_lines == ["completion_policy: allow_no_change=true"]
        spec_digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
        revision = str(issue.get("updated_at", ""))
        owner, name = repo.split("/", 1)
        data = self.api.graphql(self._CANONICAL_LINKS_QUERY,
                                {"owner": owner, "name": name, "number": number})
        issue_node = (data.get("repository") or {}).get("issue") or {}
        timeline = issue_node.get("timelineItems") or {}
        events = timeline.get("nodes") or []
        linked_numbers = set()
        page_info = timeline.get("pageInfo")
        # Never authorize from a partial relationship list. Missing pagination
        # metadata is also incomplete and therefore fails closed.
        relationship_valid = isinstance(page_info, dict) and page_info.get("hasNextPage") is False
        for event in events:
            if not isinstance(event, dict):
                relationship_valid = False
                continue
            if event.get("willCloseTarget") is True:
                source = event.get("source")
                if not isinstance(source, dict) or source.get("__typename") != "PullRequest" \
                        or not isinstance(source.get("number"), int):
                    relationship_valid = False
                else:
                    linked_numbers.add(source["number"])
        linked_numbers = sorted(linked_numbers) if relationship_valid else []
        linked_prs = [self.api.get(f"/repos/{repo}/pulls/{pr_number}") for pr_number in linked_numbers]
        open_linked = [p for p in linked_prs if p.get("state") == "open"]
        pr = linked_prs[0] if len(linked_numbers) == 1 and len(linked_prs) == 1 else None
        reviews = self._all_pages(f"/repos/{repo}/pulls/{pr['number']}/reviews?per_page=100") if pr else None
        head = pr.get("head", {}) if pr else {}
        current_sha = head.get("sha")
        current_review = None
        check_runs = ()
        if pr and pr.get("state") == "open" and current_sha:
            payload = self.api.get(f"/repos/{repo}/commits/{current_sha}/check-runs?per_page=100")
            runs = payload.get("check_runs") if isinstance(payload, dict) else None
            if not isinstance(runs, list) or payload.get("total_count", len(runs)) > len(runs):
                raise RuntimeError("required check run response is incomplete")
            check_runs = tuple(run for run in runs if isinstance(run, dict)
                               and str(run.get("head_sha", "")).casefold() == current_sha.casefold())
        if reviews is not None and pr and pr.get("state") == "open" and len(linked_numbers) == 1 and len(open_linked) == 1 and pr.get("base", {}).get("ref") == self.base_branch and \
                pr.get("user", {}).get("login", "").casefold() in self.authors:
            current_head_reviews = [r for r in reviews or []
                                    if r.get("commit_id", "").casefold() == (current_sha or "").casefold()
                                    and r.get("state") in {"APPROVED", "CHANGES_REQUESTED"}]
            if current_head_reviews:
                latest = max(current_head_reviews, key=lambda r: (r.get("submitted_at", ""), int(r.get("id", 0))))
                current_review = latest
        valid_pr = bool(pr and len(linked_numbers) == 1 and len(linked_prs) == 1
                        and pr.get("base", {}).get("ref") == self.base_branch
                        and bool(head.get("ref")) and head.get("ref") != self.base_branch
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
            formal_review_body=(current_review.get("body") or None) if current_review else None,
            canonical_relationship_valid=relationship_valid,
            canonical_link_count=len(linked_numbers),
            pr_state=pr.get("state") if valid_pr else None,
            pr_merged=bool(valid_pr and pr.get("merged") is True),
            check_runs=check_runs if valid_pr else (),
            active_labels=tuple(label for label in self.policy["active_coordination_labels"] if label in labels),
            allow_no_change=allow_no_change, frozen_spec_digest=spec_digest,
            frozen_spec_body=body, issue_title=str(issue.get("title") or ""),
        )

    def _all_pages(self, endpoint):
        separator = "&" if "?" in endpoint else "?"
        results = []
        page = 1
        while True:
            payload = self.api.get(f"{endpoint}{separator}page={page}")
            if not isinstance(payload, list):
                raise RuntimeError("GitHub formal review response is invalid")
            results.extend(payload)
            if len(payload) < 100:
                return results
            page += 1

    def close_noop_issue(self, repo, issue_number):
        # Freshly prove no canonical relationship and retain only coordination labels
        # until the Issue close itself is verified. Cleanup is a separate idempotent pass.
        fresh = self._facts(repo, issue_number)
        if (not fresh.issue_open or not fresh.frozen_spec or fresh.canceled or fresh.terminal_labels
                or not fresh.canonical_relationship_valid or fresh.canonical_link_count
                or fresh.open_linked_pr_count or "needs-human" in fresh.terminal_labels):
            return False
        return bool(self.writer.close_issue(repo, issue_number))

    def remove_coordination_labels_and_close(self, repo, issue_number):
        fresh = self._facts(repo, issue_number)
        if (not fresh.issue_open or not fresh.frozen_spec or not fresh.canonical_relationship_valid
                or fresh.canonical_link_count or fresh.open_linked_pr_count):
            return False
        if fresh.active_labels and not self.writer.replace_labels(repo, issue_number, list(fresh.active_labels), []):
            return False
        return bool(self.writer.close_issue(repo, issue_number))

    def clear_noop_coordination_labels(self, repo, issue_number):
        fresh = self._facts(repo, issue_number)
        if fresh.issue_open or not fresh.frozen_spec or fresh.canceled or not fresh.canonical_relationship_valid:
            return False
        return not fresh.active_labels or bool(self.writer.replace_labels(
            repo, issue_number, list(fresh.active_labels), []))

    def observe(self, repo, issue_number):
        return self._facts(repo, issue_number)

    def merged_closing_pr(self, repo, issue_number, pr_number):
        """Re-read one bound PR's merge through the PR itself.

        GitHub stops reporting ``willCloseTarget`` on the Issue timeline as soon as the
        merge auto-closes the Issue, which happens within seconds, so the very PR that
        closed it can no longer be seen there. The PR's own merged state and closing
        references survive closure. This read is only ever consulted for a PR number a
        durable attempt already bound, and it is not used to authorize new work.
        """
        if not isinstance(pr_number, int) or pr_number < 1:
            return None
        owner, name = repo.split("/", 1)
        data = self.api.graphql(self._MERGED_CLOSING_PR_QUERY,
                                {"owner": owner, "name": name, "number": pr_number})
        pr = ((data.get("repository") or {}).get("pullRequest")) or {}
        if not isinstance(pr, dict) or pr.get("state") != "MERGED" or pr.get("merged") is not True:
            return None
        if pr.get("baseRefName") != self.base_branch:
            return None
        head_ref, head_sha = pr.get("headRefName"), pr.get("headRefOid")
        if not isinstance(head_ref, str) or not head_ref or head_ref == self.base_branch \
                or not isinstance(head_sha, str) or not head_sha:
            return None
        references = (pr.get("closingIssuesReferences") or {}).get("nodes")
        if not isinstance(references, list) or not any(
                isinstance(node, dict) and node.get("number") == issue_number for node in references):
            return None
        return pr

    def discover_active(self, repo):
        """List only already-active recovery candidates; agent-ready is never queried."""
        import urllib.parse
        found = set()
        for label in ("agent-working", "changes-requested"):
            query = urllib.parse.urlencode({"state": "open", "labels": label, "per_page": 100})
            result = self.api.get(f"/repos/{repo}/issues?{query}")
            if not isinstance(result, list):
                raise RuntimeError("GitHub active recovery discovery returned an invalid response")
            if len(result) >= 100:
                raise RuntimeError("GitHub active recovery discovery may be incomplete; refusing partial scan")
            for issue in result:
                if isinstance(issue, dict) and "pull_request" not in issue:
                    number = issue.get("number")
                    if isinstance(number, int) and number > 0:
                        found.add(number)
        return sorted(found)

    def discover_ready(self, repo):
        """Return a lightweight candidate list; dispatch_initial owns fresh eligibility."""
        import urllib.parse
        query = urllib.parse.urlencode({"state": "open", "labels": "agent-ready", "per_page": 100})
        result = self.api.get(f"/repos/{repo}/issues?{query}")
        if not isinstance(result, list):
            raise RuntimeError("GitHub ready discovery returned an invalid response")
        if len(result) >= 100:
            raise RuntimeError("GitHub ready discovery may be incomplete; refusing partial scan")
        return sorted({item["number"] for item in result
                       if isinstance(item, dict) and "pull_request" not in item
                       and isinstance(item.get("number"), int) and item["number"] > 0})

    def transition_coordination_state(self, repo, issue_number, expected_state, new_state, expected_revision):
        before = self._facts(repo, issue_number)
        if before.revision != str(expected_revision) or before.coordination_state != expected_state or \
                before.terminal_labels or before.canceled or not before.issue_open:
            return False
        allowed_target = (new_state in self.policy["active_coordination_labels"]
                          or new_state in self.policy["terminal_coordination_labels"])
        if expected_state not in self.policy["active_coordination_labels"] or not allowed_target:
            return False
        remove = [x for x in self.policy["active_coordination_labels"] if x != new_state and x == expected_state]
        if not self.writer.replace_labels(repo, issue_number, remove, [new_state]):
            return False
        after = self._facts(repo, issue_number)
        terminal_target = new_state in self.policy["terminal_coordination_labels"]
        target_verified = after.coordination_state == new_state and (
            after.terminal_labels == (new_state,) if terminal_target else not after.terminal_labels
        )
        return after.issue_open and not after.canceled and target_verified

    def restore_repair_coordination_state(self, repo, issue_number, new_state, expected_revision):
        """Idempotently promote a grant; leave needs-human in place until target is verified."""
        before = self._facts(repo, issue_number)
        if (before.revision != str(expected_revision) and before.issue_open and before.frozen_spec
                and not before.canceled and before.coordination_state == new_state
                and before.active_labels == (new_state,) and not before.terminal_labels):
            return True
        staged = (before.terminal_labels == ("needs-human",)
                  and before.active_labels == (new_state,))
        if (before.revision != str(expected_revision)
                or before.terminal_labels != ("needs-human",) or before.canceled or not before.issue_open
                or not before.frozen_spec
                or new_state not in self.policy["active_coordination_labels"]):
            return False
        if new_state not in before.active_labels:
            if before.active_labels:
                return False
            if not self.writer.replace_labels(repo, issue_number, [], [new_state]):
                return False
            staged_facts = self._facts(repo, issue_number)
            same_evidence = (staged_facts.issue_open and staged_facts.frozen_spec
                             and not staged_facts.canceled
                             and staged_facts.pr_head_sha == before.pr_head_sha
                             and staged_facts.pr_number == before.pr_number
                             and staged_facts.pr_branch == before.pr_branch
                             and staged_facts.formal_review_state == before.formal_review_state
                             and staged_facts.formal_review_id == before.formal_review_id
                             and staged_facts.formal_review_head_sha == before.formal_review_head_sha
                             and staged_facts.check_runs == before.check_runs
                             and staged_facts.terminal_labels == ("needs-human",)
                             and staged_facts.active_labels == (new_state,))
            if not same_evidence:
                return False
        elif not staged:
            return False
        # The terminal fence remains until this final, separately retryable operation.
        if not self.writer.replace_labels(repo, issue_number, ["needs-human"], []):
            return False
        after = self._facts(repo, issue_number)
        return (after.issue_open and after.frozen_spec and not after.canceled
                and after.coordination_state == new_state and not after.terminal_labels
                and after.pr_head_sha == before.pr_head_sha and after.pr_number == before.pr_number
                and after.pr_branch == before.pr_branch)

    def clear_active_coordination_labels(self, repo, issue_number):
        """Remove active labels after a proven native merge; preserve terminal labels.

        Success is the fact this method owns: no active coordination label is left. The
        global coordination state reports a merged Issue's correct end state - no
        coordination label at all - as ``invalid``, the same value it uses for a
        contradictory label set, so that state cannot decide this result.
        """
        before = self._facts(repo, issue_number)
        active = list(before.active_labels)
        if not active:
            return True
        if not self.writer.replace_labels(repo, issue_number, active, []):
            return False
        after = self._facts(repo, issue_number)
        return not after.active_labels


def generic_prompt(request: LaunchRequest, workspace: str, policy: dict,
                   app_gh: str, app_git_push: str) -> str:
    spec = (request.frozen_spec_body or "").strip()
    common = (f"Repository: {request.repo}\nFrozen Issue: #{request.issue_number}\nWorkspace: {workspace}\n"
              f"Controlled GitHub writer: {app_gh}\nControlled git push wrapper: {app_git_push}\n\n"
              + (f"Frozen Issue body, the only product specification:\n<frozen-spec>\n{spec}\n</frozen-spec>\n\n"
                 if spec else "The Frozen Issue body could not be supplied to this launch. Do not infer the "
                              "specification; report that it is missing and stop.\n\n")
              + "How to work:\n"
              "- The Frozen Issue fixes the product goal and acceptance criteria. Confirm what the user actually "
              "needs, read the relevant code, and correct the stated root cause and file list from code evidence "
              "instead of accepting them.\n"
              "- Fix the smallest shared root cause that satisfies the Frozen Issue. When the same confirmed cause "
              "repeats in a sibling call site, component, or state reached by that evidence, fix it there too. Do "
              "not audit beyond that, and do not add validation, fallbacks, abstractions, compatibility layers, "
              "refactors, or hardening the Frozen Issue does not require.\n"
              "- Verify with evidence that distinguishes the broken behavior from the fixed one. Report exactly what "
              "you ran; never claim a check you did not run.\n"
              "- Keep a defect that is independent of this Frozen Issue out of this change. Note it in the PR body "
              "and continue. If satisfying the Frozen Issue would require changing the product goal, stop and say so.\n\n"
              "Workflow rules. Ordinary host-human gh writes are "
              "forbidden; use only the configured controlled App writer for GitHub writes. Ordinary git push "
              "is forbidden; use only the configured controlled push wrapper. Do not persist or request GitHub "
              "tokens. Do not expand scope or merge. The PR body MUST include a GitHub closing reference to this "
              "Frozen Issue, for example "
              "'Closes #<issue_number>' (or 'Fixes'/'Resolves'); a plain mention is insufficient. "
              "For PR creation use: <app-gh> pr create --title TITLE --body 'SUMMARY. Closes #<issue_number>' "
              "--head BRANCH "
              "--base BASE. For labels use: <app-gh> issue edit ISSUE --remove-label LABEL --add-label LABEL. "
              "For a push use: <app-git-push> --branch BRANCH --expected-sha FULL_SHA.\n")
    if request.attempt_kind == "repair":
        if request.repair_cause_type == "implementation_failure":
            cause_guidance = (
                "This repair was admitted for an exact-head CI failure; inspect the configured required "
                "checks on the supplied head and address the explicitly attributed implementation failure. ")
        else:
            feedback = (request.review_feedback or "").strip()
            cause_guidance = (
                "The blocking review below was submitted against this exact head and is the authority for this "
                "repair. Resolve every blocking finding it confirms"
                + (f":\n<review-findings>\n{feedback}\n</review-findings>" if feedback else
                   "; no review body was recorded, so address the formal REQUEST_CHANGES on this head.")
                + " Do not re-litigate a finding the code evidence disproves; state the evidence instead. "
                  "The Frozen Issue above is the product authority; this review supplies findings only. If a "
                  "finding conflicts with the current Frozen Issue, the Frozen Issue wins: say so instead of "
                  "reverting a product goal. "
                  "Handle only findings this review produced, not unrelated defects. ")
        return common + (f"Repair the existing PR #{request.pr_number} on existing branch {request.branch}. "
                         f"The exact supplied baseline is {request.expected_head_sha}; repair ordinal "
                         f"{request.repair_ordinal} is authoritative. {cause_guidance}Do not create another PR/branch, "
                         "count reviews, or calculate repair budget.\n")
    no_op = (f"This Frozen Issue authorizes a legitimate no-change result. If no repository change is needed, "
             f"run `{request.completion_command}` once "
             "after verifying your work. Do not describe a no-op in prose as completion. " if request.allow_no_change else "")
    pr_path = ((f"Otherwise, implement on a non-base branch and open one PR linked to this Frozen Issue, "
                f"using base branch {policy['repository']['base_branch']}.\n") if request.allow_no_change else
               (f"Implement on a non-base branch and open one PR linked to this Frozen Issue, "
                f"using base branch {policy['repository']['base_branch']}.\n"))
    return common + no_op + pr_path


class _GuardedProvider:
    """Shared attempt/provider-scoped guard installation for concrete provider adapters."""

    provider: str
    workspace: str
    app_gh: str | None
    app_git_push: str | None
    write_guard: "WorkspaceWriteGuard"
    runtime_context: dict | None

    def _guarded_environment(self, request: LaunchRequest, env: dict):
        """Install this provider's guard and return it with the controlled app-gh command."""
        if self.runtime_context is not None:
            context = dict(self.runtime_context)
            context.update(getattr(self, "launch_context_override", {}) or {})
            context.update({"repo": request.repo, "issue_number": request.issue_number,
                            "attempt_id": request.attempt_id, "provider": self.provider,
                            "allow_no_change": request.allow_no_change,
                            "authorization_digest": request.frozen_spec_digest,
                            "workspace_baseline": context.get("workspace_baseline"),
                            "database_path": context.get("database_path"),
                            "python_executable": self.runtime_context["python_executable"]})
            guard = self.write_guard.install(self.workspace, request.attempt_id, None, env,
                                             provider=self.provider, launch_context=context)
        else:
            guard = self.write_guard.install(self.workspace, request.attempt_id, self.app_git_push, env,
                                             provider=self.provider)
        env.update(guard.environment)
        app_gh_command = guard.github_command or self.app_gh
        env["VCCP_APP_GH"] = app_gh_command
        env["VCCP_APP_GIT_PUSH"] = guard.git_push_command
        env["VCCP_APP_GIT_PUSH_BACKEND"] = (
            "vccp_runtime.github_push.GitHubAppPushBackend" if self.runtime_context is not None
            else self.app_git_push
        )
        return guard, app_gh_command

    def _workspace_baseline(self):
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.workspace,
                              check=True, capture_output=True, text=True).stdout.strip()
        status = subprocess.run(["git", "status", "--porcelain=v1", "--untracked-files=all", "--", "."],
                                cwd=self.workspace, check=True, capture_output=True, text=True).stdout
        return json.dumps({"head": head, "status": status}, sort_keys=True)


_CSRF_FLAG = "--csrf_token"


def _flag_value(arguments: list[str], flag: str) -> str | None:
    """Return the value of ``--flag value`` or ``--flag=value``, else None."""
    for index, argument in enumerate(arguments):
        if argument.startswith(f"{flag}="):
            return argument.partition("=")[2] or None
        if argument == flag and index + 1 < len(arguments):
            return arguments[index + 1] or None
    return None


def _csrf_token_from(arguments: list[str]) -> str | None:
    return _flag_value(arguments, _CSRF_FLAG)


def _antigravity_endpoint_is_healthy(address: str) -> bool:
    try:
        with urlopen(Request(f"http://{address}/healthz"), timeout=2) as response:
            health = json.loads(response.read().decode("utf-8"))
    except Exception:
        return False
    return getattr(response, "status", 0) == 200 and isinstance(health, dict) \
        and health.get("status") == "ok"


def _darwin_antigravity_candidates(executable: str) -> set[tuple[str, str]]:
    try:
        processes = subprocess.run(["ps", "-axo", "pid=,command="], capture_output=True,
                                   text=True, check=False, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return set()
    if processes.returncode != 0:
        return set()

    contexts = set()
    for line in processes.stdout.splitlines():
        try:
            pid, command = line.strip().split(None, 1)
            arguments = shlex.split(command)
        except (ValueError, shlex.Error):
            continue
        if (not arguments or arguments[0] != executable or "--standalone" not in arguments
                or "--override_ide_name" not in arguments):
            continue
        ide_index = arguments.index("--override_ide_name")
        if ide_index + 1 >= len(arguments) or arguments[ide_index + 1] != "antigravity":
            continue
        csrf_token = _csrf_token_from(arguments)
        if not csrf_token:
            continue
        try:
            listeners = subprocess.run(
                ["lsof", "-nP", "-a", "-p", pid, "-iTCP", "-sTCP:LISTEN"],
                capture_output=True, text=True, check=False, timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if listeners.returncode != 0:
            continue
        ports = set(re.findall(r"TCP\s+127\.0\.0\.1:(\d+)\s+\(LISTEN\)", listeners.stdout))
        for port in ports:
            address = f"127.0.0.1:{port}"
            if _antigravity_endpoint_is_healthy(address):
                contexts.add((address, csrf_token))
    return contexts


def _windows_antigravity_candidates(executable: str) -> set[tuple[str, str]]:
    """Find the live Windows AntiGravity LS from its own process command line.

    Windows exposes no ``ps``/``lsof`` pair, so the process table is read through CIM.
    The command line already carries ``--csrf_token`` and the listening port is held by
    that same process, so no new configuration is introduced.
    """
    script = ("Get-CimInstance Win32_Process -Filter \"Name='language_server.exe'\" | "
              "Select-Object ProcessId,ExecutablePath,CommandLine | ConvertTo-Json -Compress")
    try:
        listing = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                                 capture_output=True, text=True, check=False, timeout=10,
                                 encoding="oem", errors="replace")
    except (OSError, subprocess.TimeoutExpired):
        return set()
    if listing.returncode != 0 or not (listing.stdout or "").strip():
        return set()
    try:
        processes = json.loads(listing.stdout)
    except (TypeError, ValueError):
        return set()
    if isinstance(processes, dict):
        processes = [processes]

    target = str(Path(executable).expanduser().resolve()).casefold() if executable else None
    contexts = set()
    for process in processes if isinstance(processes, list) else []:
        if not isinstance(process, dict):
            continue
        command = str(process.get("CommandLine") or "")
        if "--standalone" not in command or "--override_ide_name" not in command:
            continue
        executable_path = process.get("ExecutablePath")
        if target and isinstance(executable_path, str):
            if str(Path(executable_path).expanduser().resolve()).casefold() != target:
                continue
        arguments = [argument.strip('"') for argument in shlex.split(command, posix=False)]
        # The same executable also serves other IDE modes; only the AntiGravity
        # instance answers this AgentAPI, so the flag value matters, not its presence.
        if _flag_value(arguments, "--override_ide_name") != "antigravity":
            continue
        csrf_token = _csrf_token_from(arguments)
        if not csrf_token:
            continue
        pid = process.get("ProcessId")
        if not isinstance(pid, int):
            continue
        try:
            listeners = subprocess.run(["netstat.exe", "-ano", "-p", "TCP"],
                                       capture_output=True, text=True, check=False, timeout=10,
                                       encoding="oem", errors="replace")
        except (OSError, subprocess.TimeoutExpired):
            continue
        if listeners.returncode != 0:
            continue
        for line in (listeners.stdout or "").splitlines():
            fields = line.split()
            if len(fields) < 5 or fields[3] != "LISTENING" or fields[4] != str(pid):
                continue
            host, _, port = fields[1].rpartition(":")
            if host not in {"127.0.0.1", "0.0.0.0", "[::1]"} or not port.isdigit():
                continue
            address = f"127.0.0.1:{int(port)}"
            if _antigravity_endpoint_is_healthy(address):
                contexts.add((address, csrf_token))
    return contexts


def _discover_antigravity_agentapi_environment(executable: str) -> dict[str, str] | None:
    """Find the active AntiGravity LS endpoint without persisting its CSRF token."""
    if sys.platform == "darwin":
        contexts = _darwin_antigravity_candidates(executable)
    elif sys.platform == "win32":
        contexts = _windows_antigravity_candidates(executable)
    else:
        return None

    # Multiple live Antigravity instances are ambiguous; do not choose one by process order.
    if len(contexts) != 1:
        return None
    address, csrf_token = next(iter(contexts))
    return {"ANTIGRAVITY_LS_ADDRESS": address, "ANTIGRAVITY_CSRF_TOKEN": csrf_token}


class AntiGravityImplementer(_GuardedProvider):
    def __init__(self, executable: str, workspace: str, app_gh: str, app_git_push: str,
                 runner: Callable = subprocess.run, timeout: int = 60, environ=None, write_guard=None,
                 runtime_context: dict | None = None, conversation_roots=None,
                 project_config_root: str | Path | None = None,
                 provider: str = ANTIGRAVITY_PROVIDER):
        self.executable, self.workspace = executable, workspace
        self.app_gh, self.app_git_push = app_gh, app_git_push
        self.runner, self.timeout = runner, timeout
        self.environ = os.environ if environ is None else environ
        self.write_guard = write_guard or WorkspaceWriteGuard()
        self.runtime_context = runtime_context
        self.launch_context_override: dict | None = None
        self.provider = provider
        self.conversation_roots = [Path(p).expanduser() for p in (conversation_roots or [])]
        self.project_config_root = Path(project_config_root).expanduser() if project_config_root else (
            Path.home() / ".gemini" / "config" / "projects"
        )
        if not executable or not workspace or (runtime_context is None and (not app_gh or not app_git_push)):
            raise ValueError("language server, workspace, app-gh, and app-git-push are required")

    def _resolve_project_id(self) -> str | None:
        if not self.project_config_root or not self.project_config_root.is_dir():
            return None
        target = Path(self.workspace).resolve()
        for p in self.project_config_root.glob("*.json"):
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                continue
            resources = data.get("projectResources", {}).get("resources", [])
            for res in resources:
                uri = res.get("gitFolder", {}).get("folderUri") or res.get("folderUri")
                if uri:
                    parsed = urlparse(uri)
                    raw_path = unquote(parsed.path, encoding="utf-8")
                    if len(raw_path) > 2 and raw_path[0] == "/" and raw_path[2] == ":":
                        raw_path = raw_path[1:]
                    if Path(raw_path).resolve() == target:
                        return data.get("id")
        return None

    def launch(self, request):
        env = dict(self.environ)
        for key in list(env):
            if key.upper() in _GITHUB_CREDENTIAL_ENVIRONMENT:
                env.pop(key)
        for key in ("ANTIGRAVITY_CONVERSATION_ID", "ANTIGRAVITY_SOURCE_METADATA", "ANTIGRAVITY_TRAJECTORY_ID"):
            env.pop(key, None)
        if (sys.platform in {"darwin", "win32"} and Path(self.executable).is_file()
                and (not env.get("ANTIGRAVITY_LS_ADDRESS") or not env.get("ANTIGRAVITY_CSRF_TOKEN"))):
            context = _discover_antigravity_agentapi_environment(self.executable)
            if context is None:
                return LaunchResult(LaunchDisposition.DEFINITELY_NOT_STARTED)
            env.update(context)
        project_id = self._resolve_project_id()
        if project_id:
            env["ANTIGRAVITY_PROJECT_ID"] = project_id
        else:
            env.pop("ANTIGRAVITY_PROJECT_ID", None)
        try:
            guard, app_gh_command = self._guarded_environment(request, env)
        except Exception:
            # A missing/failed guard proves the external AgentAPI command was never invoked.
            return LaunchResult(LaunchDisposition.DEFINITELY_NOT_STARTED)
        request = __import__("dataclasses").replace(request, completion_command=guard.completion_command)
        prompt = generic_prompt(request, self.workspace, self._policy, app_gh_command, guard.git_push_command)
        try:
            result = self.runner([self.executable, "agentapi", "new-conversation", prompt],
                                 cwd=self.workspace, env=env, text=True, capture_output=True, check=False,
                                 timeout=self.timeout)
        except (FileNotFoundError, PermissionError):
            # CreateProcess failure proves AgentAPI was never invoked.
            return LaunchResult(LaunchDisposition.DEFINITELY_NOT_STARTED)
        except Exception:
            return LaunchResult(LaunchDisposition.UNKNOWN)
        if result.returncode == 0:
            identity = self._conversation_id(result.stdout)
            if identity:
                return LaunchResult(LaunchDisposition.CONFIRMED, identity)
        return LaunchResult(LaunchDisposition.UNKNOWN)

    def latest_activity(self, conversation_id: str):
        """Observe artifact mtimes; a static conversation receipt is not progress."""
        roots = list(self.conversation_roots)
        configured = self.environ.get("VCCP_ANTIGRAVITY_ROOT")
        if configured:
            roots.append(Path(configured))
        roots.append(Path.home() / ".gemini" / "antigravity")
        for key, suffix in (("LOCALAPPDATA", Path("Programs") / "antigravity"),
                            ("APPDATA", Path("Antigravity"))):
            value = self.environ.get(key)
            if value:
                roots.append(Path(value) / suffix)
        paths = []
        for root in roots:
            paths.extend((root / "conversations" / f"{conversation_id}.db",
                          root / "brain" / conversation_id / ".system_generated" / "logs" / "transcript.jsonl",
                          root / "brain" / conversation_id / ".system_generated" / "logs" / "transcript_full.jsonl",
                          root / "brain" / conversation_id / "transcript.jsonl",
                          root / "brain" / conversation_id / f"{conversation_id}.db"))
        observed = [path.stat().st_mtime for path in paths if path.is_file()]
        return max(observed) if observed else None

    @staticmethod
    def _conversation_id(stdout):
        try:
            payload = json.loads(stdout)
            for _ in range(2):
                if not isinstance(payload, str):
                    break
                payload = json.loads(payload)
        except (TypeError, ValueError):
            return None
        if not isinstance(payload, dict) or payload.get("error"):
            return None
        response = payload.get("response")
        if isinstance(response, str):
            try:
                response = json.loads(response)
            except (TypeError, ValueError):
                return None
        candidates = [payload.get("conversation_id"), payload.get("conversationId")]
        if isinstance(response, dict):
            new_conversation = response.get("newConversation")
            if isinstance(new_conversation, dict):
                candidates.append(new_conversation.get("conversationId"))
                candidates.append(new_conversation.get("conversation_id"))
            candidates.extend((response.get("conversation_id"), response.get("conversationId")))
        for candidate in candidates:
            if isinstance(candidate, str) and _CONVERSATION_UUID.fullmatch(candidate):
                return candidate.lower()
        return None

    _policy = {"repository": {"base_branch": ""}}


class ClaudeCodeImplementer(_GuardedProvider):
    """Claude Code provider: detached `claude --bg` launch with session-scoped activity.

    Claude is qualified to run unattended in the consumer workspace: `--bg` returns
    immediately with a handle for a session that outlives the VCCP runner, `agents --json`
    is the supported listing used to resolve and later re-observe that session, and the
    session's own transcript is the only available activity clock.
    """

    def __init__(self, executable: str, workspace: str, app_gh: str, app_git_push: str,
                 runner: Callable = subprocess.run, timeout: int = 60, environ=None, write_guard=None,
                 runtime_context: dict | None = None, provider: str = CLAUDE_CODE_PROVIDER,
                 permission_mode: str = "bypassPermissions", session_lookup_attempts: int = 5,
                 session_lookup_interval: float = 1.0):
        self.executable, self.workspace = executable, workspace
        self.app_gh, self.app_git_push = app_gh, app_git_push
        self.runner, self.timeout = runner, timeout
        self.environ = os.environ if environ is None else environ
        self.write_guard = write_guard or WorkspaceWriteGuard()
        self.runtime_context = runtime_context
        self.launch_context_override: dict | None = None
        self.provider = provider
        self.permission_mode = permission_mode
        self.session_lookup_attempts = session_lookup_attempts
        self.session_lookup_interval = session_lookup_interval
        if not executable or not workspace or (runtime_context is None and (not app_gh or not app_git_push)):
            raise ValueError("claude executable, workspace, app-gh, and app-git-push are required")

    def _sanitized_environment(self) -> dict:
        env = dict(self.environ)
        for key in list(env):
            if key.upper() in _GITHUB_CREDENTIAL_ENVIRONMENT or key.upper() in _CLAUDE_SESSION_ENVIRONMENT:
                env.pop(key)
        return env

    def _workspace_sessions(self, env: dict):
        """Read Claude's supported background-session listing scoped to this workspace."""
        try:
            result = self.runner([self.executable, "agents", "--json", "--all", "--cwd", self.workspace],
                                 cwd=self.workspace, env=env, text=True, encoding="utf-8",
                                 errors="replace", capture_output=True, check=False, timeout=self.timeout)
        except Exception:
            return None
        if result.returncode != 0:
            return None
        try:
            records = json.loads(result.stdout or "")
        except ValueError:
            return None
        return records if isinstance(records, list) else None

    def _resolve_execution_id(self, handle: str, env: dict) -> str | None:
        """Resolve the launch handle to the durable session UUID through Claude's own listing."""
        for attempt in range(self.session_lookup_attempts):
            for record in self._workspace_sessions(env) or ():
                if isinstance(record, dict) and record.get("id") == handle:
                    session_id = record.get("sessionId")
                    if isinstance(session_id, str) and session_id:
                        return session_id
            if attempt + 1 < self.session_lookup_attempts:
                time.sleep(self.session_lookup_interval)
        return None

    def launch(self, request):
        env = self._sanitized_environment()
        try:
            guard, app_gh_command = self._guarded_environment(request, env)
        except Exception:
            # A missing/failed guard proves the external Claude command was never invoked.
            return LaunchResult(LaunchDisposition.DEFINITELY_NOT_STARTED)
        request = __import__("dataclasses").replace(request, completion_command=guard.completion_command)
        prompt = generic_prompt(request, self.workspace, self._policy, app_gh_command, guard.git_push_command)
        command = [self.executable, "--bg", prompt, "--permission-mode", self.permission_mode,
                   "--settings", _CLAUDE_BACKGROUND_SETTINGS]
        try:
            result = self.runner(command, cwd=self.workspace, env=env, text=True, encoding="utf-8",
                                 errors="replace", capture_output=True, check=False, timeout=self.timeout)
        except (FileNotFoundError, PermissionError):
            # CreateProcess failure proves Claude was never invoked.
            return LaunchResult(LaunchDisposition.DEFINITELY_NOT_STARTED)
        except Exception:
            return LaunchResult(LaunchDisposition.UNKNOWN)
        # Past the invocation every ambiguous outcome stays UNKNOWN: a completed command
        # without a resolvable session identity cannot disprove that Claude started.
        if result.returncode != 0:
            return LaunchResult(LaunchDisposition.UNKNOWN)
        handle = _CLAUDE_BACKGROUND_HANDLE.search(result.stdout or "")
        if handle is None:
            return LaunchResult(LaunchDisposition.UNKNOWN)
        execution_id = self._resolve_execution_id(handle.group(1), env)
        if execution_id is None:
            return LaunchResult(LaunchDisposition.UNKNOWN)
        return LaunchResult(LaunchDisposition.CONFIRMED, execution_id)

    def latest_activity(self, execution_id: str):
        """Observe one Claude session passively; a session is never resumed or relaunched."""
        observed = self._workspace_sessions(self._sanitized_environment())
        if not any(isinstance(record, dict) and record.get("sessionId") == execution_id
                   for record in observed or ()):
            # Missing or unobservable provenance fails closed instead of guessing.
            return None
        root = self.environ.get("CLAUDE_CONFIG_DIR")
        projects = (Path(root).expanduser() if root else Path.home() / ".claude") / "projects"
        activity = [path.stat().st_mtime
                    for path in projects.glob(f"*/{execution_id}.jsonl") if path.is_file()]
        return max(activity) if activity else None

    _policy = {"repository": {"base_branch": ""}}


@dataclass(frozen=True)
class WorkspaceGuard:
    git_push_command: str
    environment: dict[str, str]
    github_command: str | None = None
    completion_command: str | None = None


class WorkspaceWriteGuard:
    """Install a repo-local pre-push fence and an authorized wrapper under the Git dir."""

    def __init__(self, runner: Callable = subprocess.run):
        self.runner = runner

    def install(self, workspace: str, attempt_id: str, app_git_push: str | None = None,
                inherited_environment: dict | None = None, *,
                provider: str, launch_context: dict | None = None,
                is_windows: bool | None = None) -> WorkspaceGuard:
        windows = os.name == "nt" if is_windows is None else bool(is_windows)
        result = self.runner(["git", "rev-parse", "--absolute-git-dir"], cwd=workspace,
                             text=True, capture_output=True, check=False)
        if result.returncode != 0 or not result.stdout.strip():
            raise RuntimeError("cannot locate workspace Git directory")
        git_dir = Path(result.stdout.strip())
        if not git_dir.is_absolute():
            git_dir = (Path(workspace) / git_dir).resolve()
        current_hooks = self.runner(["git", "config", "--get", "core.hooksPath"], cwd=workspace,
                                    text=True, capture_output=True, check=False)
        if current_hooks.returncode not in (0, 1):
            raise RuntimeError("cannot inspect existing Git hook configuration")
        if current_hooks.returncode == 0:
            configured_hooks = Path(current_hooks.stdout.strip())
            if not configured_hooks.is_absolute():
                configured_hooks = (Path(workspace) / configured_hooks).resolve()
            managed_root = (git_dir / "vccp-control").resolve()
            previous_hook = configured_hooks / "pre-push"
            if not configured_hooks.resolve().is_relative_to(managed_root) or not previous_hook.is_file() \
                    or "# VCCP fail-closed pre-push guard v1" not in previous_hook.read_text(encoding="utf-8"):
                raise RuntimeError("existing Git hook configuration cannot be safely composed")
        safe_id = re.sub(r"[^A-Za-z0-9._-]", "_", attempt_id)
        safe_provider = re.sub(r"[^A-Za-z0-9._-]", "_", provider)
        if not safe_id or safe_id in {".", ".."} or not safe_provider or safe_provider in {".", ".."}:
            raise ValueError("invalid launch identity for workspace guard")
        # One Runtime attempt may try several providers in fallback order, so the guard
        # root is provider-scoped: a fallback never collides with the primary's guard.
        root = git_dir / "vccp-control" / safe_id / safe_provider
        root.parent.mkdir(parents=True, exist_ok=True)
        root.mkdir(exist_ok=False)
        config_written = False
        try:
            hooks = root / "hooks"
            hooks.mkdir()
            gh_config = root / "gh-config"
            gh_config.mkdir()
            hook = hooks / "pre-push"
            hook.write_text(
                '#!/bin/sh\n'
                '# VCCP fail-closed pre-push guard v1\n'
                'if [ "${VCCP_CONTROLLED_PUSH:-}" = "1" ]; then exit 0; fi\n'
                "echo 'Direct git push is disabled; use the configured controlled push wrapper.' >&2\n"
                "exit 1\n",
                encoding="utf-8",
            )
            if launch_context is not None:
                python = str(launch_context["python_executable"])
                if not python or any(c in python for c in ('"', "%", "\r", "\n")):
                    raise ValueError("runtime Python path cannot be represented safely by the wrapper")
                runtime_root = Path(__file__).resolve().parent.parent
                context_path = root / "runtime-context.json"
                context = dict(launch_context)
                context.pop("python_executable", None)
                context["runtime_root"] = str(runtime_root)
                context_path.write_text(json.dumps(context, ensure_ascii=False), encoding="utf-8")
                if windows and any(c in str(context_path) for c in ('"', "%", "\r", "\n")):
                    raise ValueError("runtime context path cannot be represented safely by the wrapper")
                if windows and any(c in str(runtime_root) for c in ('"', "%", "\r", "\n")):
                    raise ValueError("runtime root path cannot be represented safely by the wrapper")
                github_wrapper = root / ("app-gh.cmd" if windows else "app-gh")
                wrapper = root / ("app-git-push.cmd" if windows else "app-git-push")
                completion_wrapper = root / ("complete-no-change.cmd" if windows else "complete-no-change")
                command = f'"{python}" -m vccp_runtime.controlled_cli --context "{context_path}"'
                if windows:
                    env_prefix = f'set "PYTHONPATH={runtime_root}"\r\n'
                    github_wrapper.write_text(f'@echo off\r\nsetlocal\r\n{env_prefix}'
                                              f'{command} app-gh %*\r\nexit /b %ERRORLEVEL%\r\n',
                                              encoding="utf-8")
                    wrapper.write_text(f'@echo off\r\nsetlocal\r\n{env_prefix}set "VCCP_CONTROLLED_PUSH=1"\r\n'
                                       f'{command} git-push %*\r\nexit /b %ERRORLEVEL%\r\n', encoding="utf-8")
                    completion_wrapper.write_text(f'@echo off\r\nsetlocal\r\n{env_prefix}'
                                                  f'{command} complete-no-change\r\nexit /b %ERRORLEVEL%\r\n', encoding="utf-8")
                else:
                    github_wrapper.write_text("#!/bin/sh\nexec " + command + ' app-gh "$@"\n', encoding="utf-8")
                    wrapper.write_text("#!/bin/sh\nVCCP_CONTROLLED_PUSH=1 export VCCP_CONTROLLED_PUSH\n"
                                       "exec " + command + ' git-push "$@"\n', encoding="utf-8")
                    completion_wrapper.write_text("#!/bin/sh\nexec " + command + " complete-no-change\n", encoding="utf-8")
                    github_wrapper.chmod(0o700)
                    wrapper.chmod(0o700)
                    completion_wrapper.chmod(0o700)
                    hook.chmod(0o700)
                github_command = str(github_wrapper)
            else:
                if not app_git_push:
                    raise ValueError("controlled push command is required")
                github_command = None
                wrapper = root / ("app-git-push.cmd" if windows else "app-git-push")
                if windows:
                    if any(c in app_git_push for c in ('"', "%", "\r", "\n")):
                        raise ValueError("controlled push path cannot be represented safely by the Windows shim")
                    wrapper.write_text(
                        '@echo off\r\nsetlocal\r\nset "VCCP_CONTROLLED_PUSH=1"\r\n'
                        f'call "{app_git_push}" %*\r\nexit /b %ERRORLEVEL%\r\n',
                        encoding="utf-8",
                    )
                else:
                    wrapper.write_text(
                        "#!/bin/sh\nVCCP_CONTROLLED_PUSH=1 export VCCP_CONTROLLED_PUSH\n"
                        f"exec {shlex.quote(app_git_push)} \"$@\"\n",
                        encoding="utf-8",
                    )
                wrapper.chmod(0o700)
                hook.chmod(0o700)
            configured = self.runner(["git", "config", "--local", "core.hooksPath", str(hooks)],
                                     cwd=workspace, text=True, capture_output=True, check=False)
            if configured.returncode != 0:
                maybe_configured = self.runner(["git", "config", "--local", "--get", "core.hooksPath"],
                                               cwd=workspace, text=True, capture_output=True, check=False)
                config_written = (maybe_configured.returncode == 0
                                  and Path(maybe_configured.stdout.strip()).resolve() == hooks.resolve())
                raise RuntimeError("cannot persist repo-local fail-closed Git hook")
            config_written = True
            verify = self.runner(["git", "config", "--local", "--get", "core.hooksPath"], cwd=workspace,
                                 text=True, capture_output=True, check=False)
            if verify.returncode != 0 or Path(verify.stdout.strip()).resolve() != hooks.resolve():
                raise RuntimeError("repo-local Git hook verification failed")
            try:
                config_count = int((inherited_environment or os.environ).get("GIT_CONFIG_COUNT", "0"))
            except ValueError as error:
                raise RuntimeError("invalid inherited Git config environment") from error
            env = {
                "GIT_CONFIG_COUNT": str(config_count + 1),
                f"GIT_CONFIG_KEY_{config_count}": "core.hooksPath",
                f"GIT_CONFIG_VALUE_{config_count}": str(hooks),
                "GH_CONFIG_DIR": str(gh_config),
                "GH_PROMPT_DISABLED": "1",
            }
            if launch_context is not None:
                env["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent)
            return WorkspaceGuard(str(wrapper), env, github_command,
                                  str(completion_wrapper) if launch_context is not None else None)
        except Exception:
            if not config_written:
                import shutil
                shutil.rmtree(root, ignore_errors=True)
            raise


@dataclass(frozen=True)
class RuntimeAdapters:
    core: RuntimeCore
    workflow: GitHubWorkflowAdapter
    router: ProviderRouter
    lifecycle: object
    credential_provider: GitHubAppCredentialProvider | None = None
    push_backend: object | None = None
    local_reviewer: object | None = None


def _workspace_repository(workspace: str, api_url: str) -> str:
    try:
        result = subprocess.run(["git", "remote", "get-url", "origin"], cwd=workspace,
                                text=True, capture_output=True, check=False, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        raise ValueError("workspace origin is unavailable") from None
    if result.returncode != 0:
        raise ValueError("workspace must have an origin GitHub remote")
    repo = repository_from_remote(result.stdout.strip(), api_url)
    if not repo:
        raise ValueError("workspace origin must identify a repository on the configured GitHub host")
    return repo


def _make_credential_provider(local_config: dict, target_repo: str):
    app = local_config.get("github_app")
    if not isinstance(app, dict) or not app.get("app_id"):
        raise ValueError("machine-local github_app configuration is required")
    source = app.get("credential_source")
    if not isinstance(source, dict) or source.get("type") not in ("windows_dpapi_file", "posix_file") or not source.get("path"):
        raise ValueError("github_app.credential_source must identify a Windows DPAPI file or POSIX file")
    configured_repo = app.get("target_repository")
    if configured_repo and str(configured_repo).casefold() != target_repo.casefold():
        raise ValueError("GitHub App target_repository does not match workspace origin")
    source_obj = (
        DPAPIFileCredentialSource(source["path"])
        if source.get("type") == "windows_dpapi_file"
        else POSIXFileCredentialSource(source["path"])
    )
    return GitHubAppCredentialProvider(
        app["app_id"], source_obj, target_repo,
        app.get("expected_app_slug"), app.get("api_url", "https://api.github.com"),
    )


def _configured_providers(local_config: dict) -> list[dict]:
    """Normalize machine-local provider configuration into ordered provider settings.

    The first entry is primary and later entries are fallbacks in order. The pre-P5
    AntiGravity-only configuration stays valid: without an explicit ``providers`` list,
    the existing top-level fields describe exactly one primary provider.
    """
    configured = local_config.get("providers")
    if configured is None:
        return [{
            "key": ANTIGRAVITY_PROVIDER,
            "type": ANTIGRAVITY_PROVIDER,
            "executable": local_config.get("antigravity_executable"),
            "launch_timeout_seconds": local_config.get("launch_timeout_seconds", 60),
            "conversation_roots": local_config.get("conversation_roots", []),
            "project_config_root": local_config.get("project_config_root"),
        }]
    if not isinstance(configured, list) or not configured:
        raise ValueError("machine-local providers must be a non-empty ordered list")
    for entry in configured:
        if not isinstance(entry, dict) or not isinstance(entry.get("type"), str) or not entry["type"].strip():
            raise ValueError("each machine-local provider must be an object declaring a type")
        # The key names the guard root and the durable provenance, so it is validated
        # here rather than only when the router is constructed.
        normalize_provider_key(entry.get("key"))
    return list(configured)


def _configured_providers_for_role(local_config: dict, role: str) -> list[dict]:
    """Return the configured provider set for one machine-local role.

    New deployments bind ``implementer_provider`` explicitly and get one provider,
    which makes runtime fallback impossible. Configurations without a role binding
    retain the pre-existing ordered fallback behavior for compatibility.
    """
    if role not in {"implementer", "reviewer"}:
        raise ValueError("provider role must be implementer or reviewer")
    binding = local_config.get(f"{role}_provider")
    if binding is None:
        return _configured_providers(local_config) if role == "implementer" else []
    if not isinstance(binding, str) or not binding.strip():
        raise ValueError(f"{role}_provider must name a configured provider")
    providers = _configured_providers(local_config)
    selected = [entry for entry in providers if entry.get("key") == binding]
    if len(selected) != 1:
        raise ValueError(f"{role}_provider does not name a configured provider")
    return selected


def _antigravity_provider(settings: dict, manifest: dict, workspace: str, app_gh, app_git_push,
                          runner, runtime_context):
    adapter = AntiGravityImplementer(
        settings.get("executable"), workspace, app_gh, app_git_push, runner,
        settings.get("launch_timeout_seconds", 60), runtime_context=runtime_context,
        conversation_roots=settings.get("conversation_roots", []),
        project_config_root=settings.get("project_config_root"),
        provider=settings["key"],
    )
    adapter._policy = manifest
    return adapter


def _claude_code_provider(settings: dict, manifest: dict, workspace: str, app_gh, app_git_push,
                          runner, runtime_context):
    adapter = ClaudeCodeImplementer(
        settings.get("executable"), workspace, app_gh, app_git_push, runner,
        settings.get("launch_timeout_seconds", 60), runtime_context=runtime_context,
        provider=settings["key"], permission_mode=settings.get("permission_mode", "bypassPermissions"),
    )
    adapter._policy = manifest
    return adapter


_PROVIDER_ADAPTERS = {
    ANTIGRAVITY_PROVIDER: _antigravity_provider,
    CLAUDE_CODE_PROVIDER: _claude_code_provider,
}


def _provider_adapter(settings: dict, manifest: dict, workspace: str, app_gh, app_git_push,
                      runner, runtime_context):
    """Build one concrete provider adapter from its machine-local settings."""
    factory = _PROVIDER_ADAPTERS.get(settings["type"])
    if factory is None:
        raise ValueError(f"unsupported implementer provider type: {settings['type']}")
    return factory(settings, manifest, workspace, app_gh, app_git_push, runner, runtime_context)


def build_runtime(manifest: dict, local_config: dict, api=None, writer=None,
                  runner=subprocess.run, credential_provider=None, push_backend=None):
    """Construct the Generic App-backed production graph or an explicit injected test graph."""
    if manifest.get("schema_version") != 2:
        raise ValueError("runtime wiring requires schema_version 2")
    required = ("database_path", "workspace", "owner_id")
    if any(not local_config.get(k) for k in required):
        raise ValueError("incomplete machine-local runtime configuration")
    workspace = str(Path(local_config["workspace"]).expanduser().resolve())
    app_configured = isinstance(local_config.get("github_app"), dict)
    legacy_fields = all(local_config.get(key) for key in (
        "app_gh_executable", "app_git_push_executable", "github_read_token_env"))
    if app_configured:
        app_settings = local_config["github_app"]
        api_url = app_settings.get("api_url", "https://api.github.com")
        target_repo = _workspace_repository(workspace, api_url)
        credential_provider = credential_provider or _make_credential_provider(local_config, target_repo)
        api = api or GitHubAPI.with_app_provider(credential_provider)
        writer = writer or GitHubAppWriter(
            credential_provider=credential_provider, target_repository=target_repo,
            allowed_labels=(manifest["issue_contract"]["active_coordination_labels"]
                            + manifest["issue_contract"]["terminal_coordination_labels"]),
        )
        push_backend = push_backend or GitHubAppPushBackend(
            credential_provider, workspace, target_repo,
            manifest["repository"]["base_branch"], runner=runner,
        )
        runtime_context = {
            "github_app": app_settings,
            "workspace": workspace,
            "repo": target_repo,
            "base_branch": manifest["repository"]["base_branch"],
            "coordination_labels": (manifest["issue_contract"]["active_coordination_labels"]
                                    + manifest["issue_contract"]["terminal_coordination_labels"]),
            "python_executable": sys.executable,
            "database_path": str(Path(local_config["database_path"]).expanduser().resolve()),
        }
        app_gh = app_git_push = None
    elif api is not None and legacy_fields:
        # Retained solely as the existing deterministic test injection seam.
        writer = writer or GitHubAppWriter(local_config["app_gh_executable"], runner)
        app_gh, app_git_push, runtime_context = (local_config["app_gh_executable"],
                                                 local_config["app_git_push_executable"], None)
    else:
        raise ValueError("production runtime requires the Generic github_app provider configuration")
    workflow = GitHubWorkflowAdapter(api, writer, manifest)
    router = ProviderRouter(
        (settings.get("key"), _provider_adapter(settings, manifest, workspace, app_gh,
                                                app_git_push, runner, runtime_context))
        for settings in _configured_providers_for_role(local_config, "implementer")
    )
    core = RuntimeCore(local_config["database_path"], manifest, workflow, router,
                       recovery_timeout_seconds=local_config.get("recovery_timeout_seconds", 900),
                       implementer_progress_timeout_seconds=local_config.get(
                           "implementer_progress_timeout_seconds", 1800))
    from .lifecycle import LifecycleDriver
    local_reviewer = None
    reviewer_settings = _configured_providers_for_role(local_config, "reviewer")
    if reviewer_settings:
        from .local_reviewer import GitHubUserReviewerProvider, LocalReviewerDriver, LocalReviewerWriter
        from .reviewer_agents import AntigravityReviewer, ClaudeAgentSDKReviewer
        identity = local_config.get("reviewer_github_identity")
        if not isinstance(identity, str) or not identity.strip():
            raise ValueError("explicit local Reviewer binding requires reviewer_github_identity")
        target_repo = target_repo if app_configured else _workspace_repository(
            workspace, getattr(api, "api_url", "https://api.github.com"))
        settings = reviewer_settings[0]
        if settings.get("type") == CLAUDE_AGENT_SDK_PROVIDER \
                and settings.get("key") != CLAUDE_AGENT_SDK_PROVIDER:
            raise ValueError("Claude Agent SDK Reviewer key must be claude_agent_sdk")
        reviewer_factory = {
            ANTIGRAVITY_PROVIDER: AntigravityReviewer,
            CLAUDE_AGENT_SDK_PROVIDER: ClaudeAgentSDKReviewer,
        }.get(settings.get("type"))
        if reviewer_factory is None:
            raise ValueError("unsupported local Reviewer provider type")
        review_root = local_config.get("review_workspace_root") or str(
            Path(local_config["database_path"]).expanduser().resolve().parent / "review-workspaces")
        reviewer_agent = reviewer_factory(
            settings.get("executable"), api, manifest, workspace, review_root,
            settings.get("project_config_root"),
        ) if settings.get("type") == ANTIGRAVITY_PROVIDER else reviewer_factory(
            None, api, manifest, workspace, review_root,
        )
        user_provider = GitHubUserReviewerProvider(target_repo, identity)
        reviewer_writer = LocalReviewerWriter(user_provider, workflow, target_repo, identity, manifest)
        local_reviewer = LocalReviewerDriver(core.store, reviewer_agent, reviewer_writer,
                                             settings["key"])
    lifecycle = LifecycleDriver(core, manifest, local_config.get("lifecycle_timeouts", {}),
                                local_reviewer=local_reviewer)
    return RuntimeAdapters(core, workflow, router, lifecycle,
                           credential_provider=credential_provider, push_backend=push_backend,
                           local_reviewer=local_reviewer)
