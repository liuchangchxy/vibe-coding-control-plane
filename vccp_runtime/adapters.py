"""Production adapters for GitHub and AntiGravity; RuntimeCore owns all lifecycle state."""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
from typing import Callable
from urllib.request import Request, urlopen

from .core import (LaunchDisposition, LaunchRequest, LaunchResult, RuntimeCore,
                   WorkflowSnapshot)


_CONVERSATION_UUID = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


class GitHubAPI:
    """Small read transport. The write credential boundary is the separate App writer."""
    def __init__(self, token: str, api_url: str = "https://api.github.com",
                 graphql_url: str | None = None, opener=urlopen):
        if not token:
            raise ValueError("GitHub read token is required")
        if api_url.rstrip("/") != "https://api.github.com" and not graphql_url:
            raise ValueError("custom GitHub REST API URLs require an explicit GraphQL endpoint")
        self.token, self.api_url, self.opener = token, api_url.rstrip("/"), opener
        self.graphql_url = graphql_url or "https://api.github.com/graphql"

    def get(self, path: str):
        request = Request(self.api_url + path, headers={
            "Authorization": f"Bearer {self.token}", "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        })
        with self.opener(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))

    def graphql(self, query: str, variables: dict):
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
    """Use the configured controlled app-gh drop-in for the supported label edit."""
    def __init__(self, executable: str, runner: Callable = subprocess.run):
        if not executable:
            raise ValueError("controlled GitHub App writer executable is required")
        if Path(executable).name.casefold() in {"gh", "gh.exe"}:
            raise ValueError("ordinary gh CLI cannot be used as the production coordination writer")
        self.executable, self.runner = executable, runner

    def replace_labels(self, repo: str, issue: int, remove: list[str], add: list[str]):
        command = [self.executable, "issue", "edit", str(issue), "--repo", repo]
        for label in remove:
            command.extend(("--remove-label", label))
        for label in add:
            command.extend(("--add-label", label))
        result = self.runner(command, text=True, capture_output=True, check=False)
        return result.returncode == 0


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
              "tokens.\n")
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
                 runner: Callable = subprocess.run, timeout: int = 60, environ=None, write_guard=None):
        self.executable, self.workspace = executable, workspace
        self.app_gh, self.app_git_push = app_gh, app_git_push
        self.runner, self.timeout = runner, timeout
        self.environ = os.environ if environ is None else environ
        self.write_guard = write_guard or WorkspaceWriteGuard()
        if not executable or not workspace or not app_gh or not app_git_push:
            raise ValueError("language server, workspace, app-gh, and app-git-push are required")

    def launch(self, request):
        env = dict(self.environ)
        for key in list(env):
            if key.upper() in {"GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_APP_TOKEN",
                               "GITHUB_APP_PRIVATE_KEY"}:
                env.pop(key)
        try:
            guard = self.write_guard.install(self.workspace, request.attempt_id, self.app_git_push, env)
        except Exception:
            # A missing/failed guard proves the external AgentAPI command was never invoked.
            return LaunchResult(LaunchDisposition.DEFINITELY_NOT_STARTED)
        env.update(guard.environment)
        env["VCCP_APP_GH"] = self.app_gh
        env["VCCP_APP_GIT_PUSH_BACKEND"] = self.app_git_push
        env["VCCP_APP_GIT_PUSH"] = guard.git_push_command
        prompt = generic_prompt(request, self.workspace, self._policy, self.app_gh, guard.git_push_command)
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


class WorkspaceWriteGuard:
    """Install a repo-local pre-push fence and an authorized wrapper under the Git dir."""

    def __init__(self, runner: Callable = subprocess.run):
        self.runner = runner

    def install(self, workspace: str, attempt_id: str, app_git_push: str,
                inherited_environment: dict | None = None) -> WorkspaceGuard:
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
            return WorkspaceGuard(str(wrapper), env)
        except Exception:
            if not config_written:
                import shutil
                shutil.rmtree(root, ignore_errors=True)
            raise


@dataclass(frozen=True)
class RuntimeAdapters:
    core: RuntimeCore
    workflow: GitHubWorkflowAdapter
    implementer: AntiGravityImplementer
    lifecycle: object


def build_runtime(manifest: dict, local_config: dict, api=None, writer=None, runner=subprocess.run):
    """Construct the existing core; local paths and executable settings stay machine-local."""
    if manifest.get("schema_version") != 2:
        raise ValueError("runtime wiring requires schema_version 2")
    required = ("database_path", "workspace", "owner_id", "antigravity_executable",
                "app_gh_executable", "app_git_push_executable", "github_read_token_env")
    if any(not local_config.get(k) for k in required):
        raise ValueError("incomplete machine-local runtime configuration")
    api = api or GitHubAPI(os.environ.get(local_config["github_read_token_env"], ""))
    writer = writer or GitHubAppWriter(local_config["app_gh_executable"], runner)
    workflow = GitHubWorkflowAdapter(api, writer, manifest)
    implementer = AntiGravityImplementer(local_config["antigravity_executable"], local_config["workspace"],
                                        local_config["app_gh_executable"], local_config["app_git_push_executable"], runner,
                                        local_config.get("launch_timeout_seconds", 60))
    implementer._policy = manifest
    core = RuntimeCore(local_config["database_path"], manifest, workflow, implementer,
                       recovery_timeout_seconds=local_config.get("recovery_timeout_seconds", 900))
    from .lifecycle import LifecycleDriver
    lifecycle = LifecycleDriver(core, manifest, local_config.get("lifecycle_timeouts", {}))
    return RuntimeAdapters(core, workflow, implementer, lifecycle)
