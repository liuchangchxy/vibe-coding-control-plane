"""Production adapters for GitHub and AntiGravity; RuntimeCore owns all lifecycle state."""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
from typing import Callable
from urllib.parse import quote, unquote, urlparse
from urllib.request import Request, urlopen

from .core import (LaunchDisposition, LaunchRequest, LaunchResult, RuntimeCore,
                   WorkflowSnapshot)
from .github_app import (DPAPIFileCredentialSource, GitHubAppCredentialProvider,
                         GitHubAppError, GitHubAppIdentityError, GitHubAppPermissionError,
                         GitHubHTTPError, repository_from_remote)
from .github_push import GitHubAppPushBackend
from .providers import ANTIGRAVITY_PROVIDER, ProviderRouter


_CONVERSATION_UUID = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


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
                raise GitHubHTTPError(status, "GET")
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
        reviews = self.api.get(f"/repos/{repo}/pulls/{pr['number']}/reviews?per_page=100") if pr else None
        reviews_complete = isinstance(reviews, list) and len(reviews) < 100
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
        if reviews_complete and pr and pr.get("state") == "open" and len(linked_numbers) == 1 and len(open_linked) == 1 and pr.get("base", {}).get("ref") == self.base_branch and \
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
            canonical_relationship_valid=relationship_valid,
            canonical_link_count=len(linked_numbers),
            pr_state=pr.get("state") if valid_pr else None,
            pr_merged=bool(valid_pr and pr.get("merged") is True),
            check_runs=check_runs if valid_pr else (),
            active_labels=tuple(label for label in self.policy["active_coordination_labels"] if label in labels),
        )

    def observe(self, repo, issue_number):
        return self._facts(repo, issue_number)

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

    def clear_active_coordination_labels(self, repo, issue_number):
        """Remove active labels after a proven native merge; preserve terminal labels."""
        before = self._facts(repo, issue_number)
        active = list(before.active_labels)
        if not active:
            return before.coordination_state != "invalid"
        if not self.writer.replace_labels(repo, issue_number, active, []):
            return False
        after = self._facts(repo, issue_number)
        return after.coordination_state != "invalid" and \
            after.coordination_state not in self.policy["active_coordination_labels"]


def generic_prompt(request: LaunchRequest, workspace: str, policy: dict,
                   app_gh: str, app_git_push: str) -> str:
    common = (f"Repository: {request.repo}\nFrozen Issue: #{request.issue_number}\nWorkspace: {workspace}\n"
              "Read the Frozen Issue from GitHub and treat it as the sole task specification. "
              f"Controlled GitHub writer: {app_gh}\nControlled git push wrapper: {app_git_push}\n"
              "Do not expand scope or merge. Report tests actually run. Ordinary host-human gh writes are "
              "forbidden; use only the configured controlled App writer for GitHub writes. Ordinary git push "
              "is forbidden; use only the configured controlled push wrapper. Do not persist or request GitHub "
              "tokens. The PR body MUST include a GitHub closing reference to this Frozen Issue, for example "
              "'Closes #<issue_number>' (or 'Fixes'/'Resolves'); a plain mention is insufficient. "
              "For PR creation use: <app-gh> pr create --title TITLE --body 'SUMMARY. Closes #<issue_number>' "
              "--head BRANCH "
              "--base BASE. For labels use: <app-gh> issue edit ISSUE --remove-label LABEL --add-label LABEL. "
              "For a push use: <app-git-push> --branch BRANCH --expected-sha FULL_SHA.\n")
    if request.attempt_kind == "repair":
        cause_guidance = ("This repair was admitted for an exact-head CI failure; inspect the configured required "
                          "checks on the supplied head and address the explicitly attributed implementation failure. "
                          if request.repair_cause_type == "implementation_failure" else "")
        return common + (f"Repair the existing PR #{request.pr_number} on existing branch {request.branch}. "
                         f"The exact supplied baseline is {request.expected_head_sha}; repair ordinal "
                         f"{request.repair_ordinal} is authoritative. {cause_guidance}Do not create another PR/branch, "
                         "count reviews, or calculate repair budget.\n")
    return common + (f"Implement on a non-base branch and open one PR linked to this Frozen Issue, "
                     f"using base branch {policy['repository']['base_branch']}.\n")


class AntiGravityImplementer:
    def __init__(self, executable: str, workspace: str, app_gh: str, app_git_push: str,
                 runner: Callable = subprocess.run, timeout: int = 60, environ=None, write_guard=None,
                 runtime_context: dict | None = None, conversation_roots=None,
                 project_config_root: str | Path | None = None):
        self.executable, self.workspace = executable, workspace
        self.app_gh, self.app_git_push = app_gh, app_git_push
        self.runner, self.timeout = runner, timeout
        self.environ = os.environ if environ is None else environ
        self.write_guard = write_guard or WorkspaceWriteGuard()
        self.runtime_context = runtime_context
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
            if key.upper() in {"GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_APP_TOKEN",
                               "GITHUB_APP_PRIVATE_KEY"}:
                env.pop(key)
        for key in ("ANTIGRAVITY_CONVERSATION_ID", "ANTIGRAVITY_SOURCE_METADATA", "ANTIGRAVITY_TRAJECTORY_ID"):
            env.pop(key, None)
        project_id = self._resolve_project_id()
        if project_id:
            env["ANTIGRAVITY_PROJECT_ID"] = project_id
        else:
            env.pop("ANTIGRAVITY_PROJECT_ID", None)
        try:
            launch_context = None
            if self.runtime_context is not None:
                launch_context = dict(self.runtime_context)
                launch_context.update({"repo": request.repo, "issue_number": request.issue_number,
                                       "python_executable": self.runtime_context["python_executable"]})
            if launch_context is None:
                guard = self.write_guard.install(self.workspace, request.attempt_id, self.app_git_push, env)
            else:
                guard = self.write_guard.install(self.workspace, request.attempt_id, None, env,
                                                 launch_context=launch_context)
        except Exception:
            # A missing/failed guard proves the external AgentAPI command was never invoked.
            return LaunchResult(LaunchDisposition.DEFINITELY_NOT_STARTED)
        env.update(guard.environment)
        app_gh_command = guard.github_command or self.app_gh
        env["VCCP_APP_GH"] = app_gh_command
        env["VCCP_APP_GIT_PUSH_BACKEND"] = (
            "vccp_runtime.github_push.GitHubAppPushBackend" if self.runtime_context is not None
            else self.app_git_push
        )
        env["VCCP_APP_GIT_PUSH"] = guard.git_push_command
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


@dataclass(frozen=True)
class WorkspaceGuard:
    git_push_command: str
    environment: dict[str, str]
    github_command: str | None = None


class WorkspaceWriteGuard:
    """Install a repo-local pre-push fence and an authorized wrapper under the Git dir."""

    def __init__(self, runner: Callable = subprocess.run):
        self.runner = runner

    def install(self, workspace: str, attempt_id: str, app_git_push: str | None = None,
                inherited_environment: dict | None = None, *,
                launch_context: dict | None = None) -> WorkspaceGuard:
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
        if not safe_id or safe_id in {".", ".."}:
            raise ValueError("invalid launch identity for workspace guard")
        root = git_dir / "vccp-control" / safe_id
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
                context_path = root / "runtime-context.json"
                context = dict(launch_context)
                context.pop("python_executable", None)
                context_path.write_text(json.dumps(context, ensure_ascii=False), encoding="utf-8")
                if os.name == "nt" and any(c in str(context_path) for c in ('"', "%", "\r", "\n")):
                    raise ValueError("runtime context path cannot be represented safely by the wrapper")
                github_wrapper = root / ("app-gh.cmd" if os.name == "nt" else "app-gh")
                wrapper = root / ("app-git-push.cmd" if os.name == "nt" else "app-git-push")
                command = f'"{python}" -m vccp_runtime.controlled_cli --context "{context_path}"'
                if os.name == "nt":
                    github_wrapper.write_text(f'@echo off\r\n{command} app-gh %*\r\nexit /b %ERRORLEVEL%\r\n',
                                              encoding="utf-8")
                    wrapper.write_text('@echo off\r\nsetlocal\r\nset "VCCP_CONTROLLED_PUSH=1"\r\n'
                                       f'{command} git-push %*\r\nexit /b %ERRORLEVEL%\r\n', encoding="utf-8")
                else:
                    github_wrapper.write_text("#!/bin/sh\nexec " + command + ' app-gh "$@"\n', encoding="utf-8")
                    wrapper.write_text("#!/bin/sh\nVCCP_CONTROLLED_PUSH=1 export VCCP_CONTROLLED_PUSH\n"
                                       "exec " + command + ' git-push "$@"\n', encoding="utf-8")
                    github_wrapper.chmod(0o700)
                    wrapper.chmod(0o700)
                    hook.chmod(0o700)
                github_command = str(github_wrapper)
            else:
                if not app_git_push:
                    raise ValueError("controlled push command is required")
                github_command = None
                wrapper = root / ("app-git-push.cmd" if os.name == "nt" else "app-git-push")
                if os.name == "nt":
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
            return WorkspaceGuard(str(wrapper), env, github_command)
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
    if not isinstance(source, dict) or source.get("type") != "windows_dpapi_file" or not source.get("path"):
        raise ValueError("github_app.credential_source must identify a Windows DPAPI file")
    configured_repo = app.get("target_repository")
    if configured_repo and str(configured_repo).casefold() != target_repo.casefold():
        raise ValueError("GitHub App target_repository does not match workspace origin")
    return GitHubAppCredentialProvider(
        app["app_id"], DPAPIFileCredentialSource(source["path"]), target_repo,
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
    return list(configured)


def _provider_adapter(settings: dict, manifest: dict, workspace: str, app_gh, app_git_push,
                      runner, runtime_context):
    """Build one concrete provider adapter from its machine-local settings."""
    if settings["type"] != ANTIGRAVITY_PROVIDER:
        raise ValueError(f"unsupported implementer provider type: {settings['type']}")
    adapter = AntiGravityImplementer(
        settings.get("executable"), workspace, app_gh, app_git_push, runner,
        settings.get("launch_timeout_seconds", 60), runtime_context=runtime_context,
        conversation_roots=settings.get("conversation_roots", []),
        project_config_root=settings.get("project_config_root"),
    )
    adapter._policy = manifest
    return adapter


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
        for settings in _configured_providers(local_config)
    )
    core = RuntimeCore(local_config["database_path"], manifest, workflow, router,
                       recovery_timeout_seconds=local_config.get("recovery_timeout_seconds", 900),
                       implementer_progress_timeout_seconds=local_config.get(
                           "implementer_progress_timeout_seconds", 1800))
    from .lifecycle import LifecycleDriver
    lifecycle = LifecycleDriver(core, manifest, local_config.get("lifecycle_timeouts", {}))
    return RuntimeAdapters(core, workflow, router, lifecycle,
                           credential_provider=credential_provider, push_backend=push_backend)
