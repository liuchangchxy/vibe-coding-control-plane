"""Exact-head native review writes for the machine-local Reviewer role."""
from __future__ import annotations

import re
import json
import os
import subprocess
from urllib.request import Request, urlopen


_SHA = re.compile(r"^[0-9a-f]{40}$", re.IGNORECASE)
_ENABLE_AUTO_MERGE = """
mutation EnableVCCPNativeAutoMerge($pullRequestId: ID!, $mergeMethod: PullRequestMergeMethod!) {
  enablePullRequestAutoMerge(input: {pullRequestId: $pullRequestId, mergeMethod: $mergeMethod}) {
    pullRequest { number }
  }
}
"""


class LocalReviewerWriter:
    """Submit only an exact-head formal decision after fresh contract/CI checks."""

    def __init__(self, provider, workflow, target_repository: str,
                 reviewer_login: str, manifest: dict):
        if not target_repository or not reviewer_login:
            raise ValueError("local Reviewer repository and GitHub identity are required")
        if manifest.get("schema_version") != 2:
            raise ValueError("local Reviewer requires a schema-v2 manifest")
        self.provider = provider
        self.workflow = workflow
        self.target_repository = target_repository.casefold()
        self.reviewer_login = reviewer_login.casefold()
        self.manifest = manifest

    def _snapshot(self, repo: str, issue: int, pr: int, head: str):
        snapshot = self.workflow.observe(repo, issue)
        contract = self.manifest["issue_contract"]
        required = self.manifest["reviewer"]["required_checks"]
        if (not snapshot.canonical_relationship_valid or snapshot.canonical_link_count != 1
                or snapshot.pr_number != pr or snapshot.pr_linked_issue != issue
                or not snapshot.issue_open or not snapshot.pr_open or not snapshot.frozen_spec
                or snapshot.terminal_labels or snapshot.canceled
                or (snapshot.pr_head_sha or "").casefold() != head.casefold()):
            raise ValueError("Reviewer preconditions or exact head SHA changed")
        for check in required:
            runs = [run for run in snapshot.check_runs
                    if run.get("name") == check["name"]
                    and str(run.get("head_sha", "")).casefold() == head.casefold()]
            if not runs:
                raise ValueError("required checks are not accepted on the exact head SHA")
            latest = max(runs, key=lambda item: (str(item.get("started_at") or ""),
                                                int(item.get("id") or 0)))
            if (latest.get("status") != "completed"
                    or latest.get("conclusion") not in check["accepted_conclusions"]):
                raise ValueError("required checks are not accepted on the exact head SHA")
        return snapshot

    def _pull_request(self, repo: str, pr: int, head: str):
        response = self.provider.request("GET", f"/repos/{repo}/pulls/{pr}", repo)
        status, data = response
        if status != 200 or not isinstance(data, dict):
            raise ValueError("current pull request could not be verified")
        author = str((data.get("user") or {}).get("login") or "").casefold()
        allowed = {str(item).casefold() for item in self.manifest["repository"]["implementer_authors"]}
        if author == self.reviewer_login:
            raise ValueError("reviewer identity matches PR author")
        if (data.get("state") != "open" or data.get("draft") is True
                or (data.get("base") or {}).get("ref") != self.manifest["repository"]["base_branch"]
                or author not in allowed):
            raise ValueError("pull request does not meet the configured Reviewer contract")
        if str((data.get("head") or {}).get("sha") or "").casefold() != head.casefold():
            raise ValueError("pull request head SHA changed")
        return data

    def _existing_decision(self, repo: str, pr: int, head: str):
        status, reviews = self.provider.request(
            "GET", f"/repos/{repo}/pulls/{pr}/reviews?per_page=100", repo)
        if status != 200 or not isinstance(reviews, list) or len(reviews) >= 100:
            raise ValueError("formal review state is incomplete")
        return next((review for review in reviews
                     if str((review.get("user") or {}).get("login") or "").casefold() == self.reviewer_login
                     and str(review.get("commit_id") or "").casefold() == head.casefold()
                     and review.get("state") in {"APPROVED", "CHANGES_REQUESTED"}), None)

    def _enable_auto_merge(self, repo: str, pull_request: dict):
        method = str(self.manifest["merge"]["method"]).upper()
        if method not in {"MERGE", "SQUASH", "REBASE"}:
            raise ValueError("manifest merge method is unsupported")
        status, data = self.provider.request("POST", "/graphql", repo, {
            "query": _ENABLE_AUTO_MERGE,
            "variables": {"pullRequestId": pull_request["node_id"], "mergeMethod": method},
        })
        payload = (data or {}).get("data", {}).get("enablePullRequestAutoMerge") if isinstance(data, dict) else None
        if status != 200 or not isinstance(payload, dict) or not isinstance(payload.get("pullRequest"), dict):
            raise ValueError("GitHub native Auto-merge could not be enabled")

    def submit(self, issue_number: int, pr_number: int, head_sha: str,
               event: str, body: str) -> dict:
        repo = self.target_repository
        if (not isinstance(issue_number, int) or issue_number < 1
                or not isinstance(pr_number, int) or pr_number < 1
                or not isinstance(head_sha, str) or not _SHA.fullmatch(head_sha)
                or event not in {"APPROVE", "REQUEST_CHANGES"}
                or not isinstance(body, str) or not body.strip()):
            raise ValueError("Reviewer decision arguments are invalid")

        snapshot = self._snapshot(repo, issue_number, pr_number, head_sha)
        pull_request = self._pull_request(repo, pr_number, head_sha)
        existing = self._existing_decision(repo, pr_number, head_sha)
        if existing:
            if existing["state"] == "CHANGES_REQUESTED" and snapshot.coordination_state == "agent-working":
                self.workflow.transition_coordination_state(
                    repo, issue_number, "agent-working", "changes-requested", snapshot.revision)
            return {"status": "already_reviewed", "head_sha": head_sha.lower(),
                    "state": existing["state"]}

        if event == "APPROVE":
            self._enable_auto_merge(repo, pull_request)

        # Rebuild authority immediately before the formal write; stale work never
        # receives a review against a later head.
        current = self._snapshot(repo, issue_number, pr_number, head_sha)
        self._pull_request(repo, pr_number, head_sha)
        status, response = self.provider.request(
            "POST", f"/repos/{repo}/pulls/{pr_number}/reviews", repo,
            {"commit_id": head_sha.lower(), "event": event, "body": body.strip()},
        )
        if status != 200 and status != 201:
            raise ValueError("GitHub did not accept the exact-head formal review")
        if event == "REQUEST_CHANGES" and current.coordination_state == "agent-working":
            changed = self.workflow.transition_coordination_state(
                repo, issue_number, "agent-working", "changes-requested", current.revision)
            if not changed:
                return {"status": "review_submitted_coordination_pending", "head_sha": head_sha.lower(),
                        "review_id": (response or {}).get("id")}
        return {"status": "review_submitted", "head_sha": head_sha.lower(),
                "review_id": (response or {}).get("id"), "event": event}


class GitHubUserReviewerProvider:
    """Current-user GitHub access fenced to one repository and Reviewer operations."""

    def __init__(self, target_repository: str, expected_login: str,
                 runner=subprocess.run, opener=urlopen, api_url="https://api.github.com"):
        if not re.fullmatch(r"[^/]+/[^/]+", target_repository or "") or not expected_login:
            raise ValueError("Reviewer GitHub identity and target repository are required")
        self.target_repository = target_repository.casefold()
        self.expected_login = expected_login.casefold()
        self.runner, self.opener, self.api_url = runner, opener, api_url.rstrip("/")
        self._token = None

    def _get_token(self):
        if self._token:
            return self._token
        result = self.runner(["gh", "auth", "token", "--hostname", "github.com"],
                             text=True, capture_output=True, check=False, timeout=15)
        token = (getattr(result, "stdout", "") or "").strip()
        if getattr(result, "returncode", 1) != 0 or not token:
            raise ValueError("authenticated GitHub user credential is unavailable")
        request = Request(self.api_url + "/user", headers={
            "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        })
        try:
            with self.opener(request, timeout=15) as response:
                identity = json.loads(response.read().decode("utf-8"))
        except Exception:
            raise ValueError("authenticated GitHub user identity could not be verified") from None
        if str(identity.get("login") or "").casefold() != self.expected_login:
            raise ValueError("authenticated GitHub user does not match configured Reviewer identity")
        self._token = token
        return token

    def verify_identity(self):
        self._get_token()
        return {"login": self.expected_login, "verified": True}

    def request(self, method: str, path: str, repo: str, payload=None):
        if repo.casefold() != self.target_repository:
            raise ValueError("Reviewer GitHub request is outside the configured Consumer")
        allowed = (
            method == "GET" and re.fullmatch(r"/repos/[^/]+/[^/]+/pulls/\d+", path)
            or method == "GET" and re.fullmatch(r"/repos/[^/]+/[^/]+/pulls/\d+/reviews\?per_page=100", path)
            or method == "POST" and re.fullmatch(r"/repos/[^/]+/[^/]+/pulls/\d+/reviews", path)
            or method == "POST" and path == "/graphql"
        )
        if not allowed:
            raise ValueError("GitHub user provider operation is outside the Reviewer allowlist")
        if path == "/graphql" and "EnableVCCPNativeAutoMerge" not in str((payload or {}).get("query", "")):
            raise ValueError("GitHub GraphQL operation is outside the Reviewer allowlist")
        token = self._get_token()
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        url = self.api_url + ("/graphql" if path == "/graphql" else path)
        request = Request(url, data=data, method=method, headers={
            "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
            "Content-Type": "application/json", "X-GitHub-Api-Version": "2022-11-28",
        })
        try:
            with self.opener(request, timeout=30) as response:
                content = response.read()
                return response.status, json.loads(content.decode("utf-8")) if content else {}
        except Exception as error:
            code = getattr(error, "code", None)
            if code:
                try:
                    return int(code), json.loads(error.read().decode("utf-8"))
                except Exception:
                    return int(code), {}
            raise ValueError("GitHub Reviewer request failed") from None


class LocalReviewerDriver:
    """Lifecycle hook for one durable, exact-head local Reviewer execution."""

    def __init__(self, store, provider, writer, provider_key: str):
        if not provider_key:
            raise ValueError("an explicit Local Reviewer provider binding is required")
        self.store, self.provider, self.writer = store, provider, writer
        self.provider_key = provider_key

    def advance(self, repo: str, issue: int, pr: int, head: str):
        try:
            self.writer._snapshot(repo, issue, pr, head)
            self.writer._pull_request(repo, pr, head)
        except ValueError:
            return {"status": "reviewer_preconditions_changed", "head_sha": head}
        existing = self.writer._existing_decision(repo, pr, head)
        if existing:
            self.store.update_reviewer_execution(repo, pr, head, "DECIDED",
                                                 outcome=existing["state"])
            return {"status": "formal_review_exists", "state": existing["state"],
                    "head_sha": head}
        record, claimed = self.store.claim_reviewer_execution(
            repo, pr, head, self.provider_key)
        if not claimed:
            if record["state"] == "RUNNING" and record.get("execution_id"):
                try:
                    result = self.provider.observe_review(record["execution_id"], repo, issue, pr, head)
                except Exception:
                    self.store.update_reviewer_execution(repo, pr, head, "FAILED",
                                                         outcome="execution_observation_failed")
                    return {"status": "reviewer_execution_failed", "head_sha": head}
                if result.get("status") == "running":
                    return {"status": "reviewer_running", "execution_id": record["execution_id"],
                            "head_sha": head}
                if result.get("status") != "completed":
                    self.store.update_reviewer_execution(repo, pr, head, "FAILED",
                                                         outcome="execution_unobservable_or_failed")
                    return {"status": "reviewer_execution_failed", "head_sha": head}
                return self._submit_decision(repo, issue, pr, head, result)
            if record["state"] == "STARTING":
                recover = getattr(self.provider, "recover_review", None)
                try:
                    execution_id = recover(repo, issue, pr, head) if recover else None
                except Exception:
                    execution_id = None
                if execution_id:
                    self.store.update_reviewer_execution(repo, pr, head, "RUNNING",
                                                         str(execution_id))
                    try:
                        observed = self.provider.observe_review(str(execution_id), repo, issue,
                                                               pr, head)
                    except Exception:
                        observed = {"status": "failed"}
                    if observed.get("status") == "running":
                        return {"status": "reviewer_running", "execution_id": str(execution_id),
                                "head_sha": head}
                    if observed.get("status") == "completed":
                        return self._submit_decision(repo, issue, pr, head, observed)
                    self.store.update_reviewer_execution(repo, pr, head, "FAILED",
                                                         outcome="recovered_execution_unobservable")
                    return {"status": "reviewer_execution_failed", "head_sha": head}
                self.store.update_reviewer_execution(repo, pr, head, "FAILED",
                                                     outcome="runner_interrupted_during_launch")
                return {"status": "reviewer_launch_interrupted", "head_sha": head}
            return {"status": "reviewer_not_relaunched", "state": record["state"],
                    "outcome": record.get("outcome"), "head_sha": head}

        try:
            execution = self.provider.launch_review(repo, issue, pr, head)
        except Exception:
            self.store.update_reviewer_execution(repo, pr, head, "FAILED",
                                                 outcome="provider_launch_failed")
            return {"status": "reviewer_launch_failed", "head_sha": head}
        if not isinstance(execution, dict) or not execution.get("execution_id"):
            self.store.update_reviewer_execution(repo, pr, head, "FAILED",
                                                 outcome="provider_launch_unobservable")
            return {"status": "reviewer_launch_unobservable", "head_sha": head}
        self.store.update_reviewer_execution(repo, pr, head, "RUNNING",
                                             str(execution["execution_id"]))
        return {"status": "reviewer_running", "execution_id": str(execution["execution_id"]),
                "head_sha": head}

    def _submit_decision(self, repo, issue, pr, head, result):
        event, body = result.get("event"), result.get("body")
        if event not in {"APPROVE", "REQUEST_CHANGES"} or not isinstance(body, str) or not body.strip():
            self.store.update_reviewer_execution(repo, pr, head, "FAILED",
                                                 outcome="invalid_reviewer_decision")
            return {"status": "reviewer_invalid_decision", "head_sha": head}
        try:
            decision = self.writer.submit(issue, pr, head, event, body)
        except ValueError:
            self.store.update_reviewer_execution(repo, pr, head, "FAILED",
                                                 outcome="exact_head_or_github_write_rejected")
            return {"status": "reviewer_decision_rejected", "head_sha": head}
        state = "DECIDED" if decision.get("status") in {"review_submitted", "already_reviewed"} else "COMPLETED"
        self.store.update_reviewer_execution(repo, pr, head, state, outcome=event)
        return {"status": "review_decision_submitted", "event": event,
                "head_sha": head, "decision_status": decision.get("status")}
