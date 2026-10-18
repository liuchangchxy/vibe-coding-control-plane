"""Provider-specific local Reviewer sessions on disposable exact-PR workspaces."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import threading
import time
from urllib.request import Request, urlopen
from uuid import uuid4

from .adapters import _CLAUDE_BACKGROUND_HANDLE, _CLAUDE_SESSION_ENVIRONMENT
from .runtime_checkout import _SHA


def _run(command, cwd=None, env=None, timeout=30):
    return subprocess.run(command, cwd=cwd, env=env, text=True, encoding="utf-8",
                          errors="replace", capture_output=True, check=False, timeout=timeout)


def _decision(text: str) -> dict | None:
    if not isinstance(text, str):
        return None
    for match in reversed(list(re.finditer(r"\{\s*\"event\"\s*:", text))):
        try:
            value, _ = json.JSONDecoder().raw_decode(text[match.start():])
        except ValueError:
            continue
        if value.get("event") in {"APPROVE", "REQUEST_CHANGES"} \
                and isinstance(value.get("body"), str) and value["body"].strip():
            return {"event": value["event"], "body": value["body"].strip()}
    return None


class ReviewerTaskError(RuntimeError):
    """Bounded Reviewer failure context with no response body or credential data."""

    def __init__(self, stage: str, endpoint: str | None = None, http_status: int | None = None,
                 exception_type: str = "RuntimeError", process_status: int | None = None):
        self.stage = stage
        self.endpoint = endpoint
        self.http_status = http_status
        self.exception_type = exception_type
        self.process_status = process_status
        fields = [f"stage={stage}", f"endpoint={endpoint or 'none'}",
                  f"http_status={http_status if http_status is not None else 'none'}",
                  f"exception_type={exception_type}"]
        if process_status is not None:
            fields.append(f"process_status={process_status}")
        super().__init__("Reviewer launch failed: " + " ".join(fields))


class _ReviewWorkspace:
    def __init__(self, source: str, root: str):
        self.source = Path(source).expanduser().resolve()
        self.root = Path(root).expanduser().resolve()

    def path_for(self, repo: str, pr: int, head: str) -> Path:
        safe_repo = re.sub(r"[^A-Za-z0-9_.-]", "_", repo)
        return self.root / safe_repo / str(pr) / head.lower()

    def prepare(self, repo: str, pr: int, head: str) -> Path:
        if not _SHA.fullmatch(head):
            raise ValueError("Reviewer workspace requires a full head SHA")
        fetched = _run(["git", "fetch", "--no-tags", "origin", f"refs/pull/{pr}/head"], self.source)
        if fetched.returncode != 0:
            raise ValueError("exact pull request head could not be fetched")
        actual = _run(["git", "rev-parse", "FETCH_HEAD"], self.source)
        if actual.returncode != 0 or actual.stdout.strip().lower() != head.lower():
            raise ValueError("fetched pull request head does not match captured SHA")
        path = self.path_for(repo, pr, head)
        if path.exists():
            if path.is_symlink() or not path.is_dir():
                raise ValueError("Reviewer workspace path is not a real directory")
            current = _run(["git", "rev-parse", "HEAD"], path)
            status = _run(["git", "status", "--porcelain=v1", "--untracked-files=all"], path)
            if current.returncode or current.stdout.strip().lower() != head.lower() or status.stdout.strip():
                raise ValueError("existing Reviewer workspace is not clean at the exact head")
            return path
        path.parent.mkdir(parents=True, exist_ok=True)
        added = _run(["git", "worktree", "add", "--detach", str(path), head.lower()], self.source)
        if added.returncode != 0:
            raise ValueError("exact-head Reviewer workspace could not be created")
        return path

    def remove_if_clean(self, repo: str, pr: int, head: str):
        path = self.path_for(repo, pr, head)
        if not path.exists() or path.is_symlink():
            return
        status = _run(["git", "status", "--porcelain=v1", "--untracked-files=all"], path)
        if status.returncode == 0 and not status.stdout.strip():
            _run(["git", "worktree", "remove", str(path)], self.source)


class _ReviewerTask:
    def __init__(self, api, manifest, contract_path: Path, workspace: _ReviewWorkspace):
        self.api, self.manifest = api, manifest
        self.contract_path, self.workspace = contract_path, workspace

    def _get(self, stage, endpoint):
        try:
            return self.api.get(endpoint)
        except Exception as error:
            actual_endpoint = getattr(error, "endpoint", None)
            diagnostic_stage = f"{stage}.github_app_auth" if actual_endpoint and actual_endpoint != endpoint else stage
            raise ReviewerTaskError(
                diagnostic_stage, actual_endpoint or endpoint, getattr(error, "status", None),
                getattr(error, "transport_exception_type", None) or type(error).__name__,
            ) from None

    def build(self, repo, issue, pr, head):
        issue_endpoint = f"/repos/{repo}/issues/{issue}"
        pull_endpoint = f"/repos/{repo}/pulls/{pr}"
        snapshot_issue = self._get("issue", issue_endpoint)
        pull = self._get("pull_request", pull_endpoint)
        if (pull.get("state") != "open" or pull.get("draft") is True
                or (pull.get("base") or {}).get("ref") != self.manifest["repository"]["base_branch"]
                or str((pull.get("user") or {}).get("login") or "").casefold()
                not in {str(name).casefold() for name in self.manifest["repository"]["implementer_authors"]}
                or str((pull.get("head") or {}).get("sha") or "").casefold() != head.casefold()):
            raise ValueError("Reviewer task PR state or exact head changed")
        reviews_endpoint = f"/repos/{repo}/pulls/{pr}/reviews?per_page=100"
        reviews = self._get("reviews", reviews_endpoint)
        if not isinstance(reviews, list) or len(reviews) >= 100:
            raise ValueError("formal review history is incomplete")
        files = []
        for page in range(1, 31):
            files_endpoint = f"/repos/{repo}/pulls/{pr}/files?per_page=100&page={page}"
            current = self._get("files", files_endpoint)
            if not isinstance(current, list):
                raise ValueError("pull request changed-file list is incomplete")
            files.extend(current)
            if len(current) < 100: break
        else:
            raise ValueError("pull request changed-file list exceeded the bounded review limit")
        if not files:
            raise ValueError("pull request has no observable changed files")
        try:
            path = self.workspace.prepare(repo, pr, head)
        except Exception as error:
            raise ReviewerTaskError("review_workspace", exception_type=type(error).__name__) from None
        contract = self.contract_path.read_text(encoding="utf-8")
        issue_body = str(snapshot_issue.get("body") or "")
        title = str(pull.get("title") or "")
        history = "\n".join(
            f"- {item.get('state')} at {item.get('commit_id')}: {item.get('body') or '(empty review body)'}"
            for item in reviews if item.get("state") in {"APPROVED", "CHANGES_REQUESTED"}
        ) or "(no prior formal review)"
        change_list = "\n\n".join(
            f"### {item.get('filename')} ({item.get('status')}, +{item.get('additions', 0)}/-{item.get('deletions', 0)})\n"
            f"```diff\n{item.get('patch') or '[patch unavailable; inspect the exact-head workspace]'}\n```"
            for item in files
        )
        return path, (f"You are VCCP's local Reviewer for exactly {repo} PR #{pr} at head {head}.\n"
                      f"Disposable, detached review workspace: {path}\n"
                      f"PR title: {title}\nFrozen Issue #{issue} body follows; it is the sole product scope:\n"
                      f"<frozen-issue>\n{issue_body}\n</frozen-issue>\n\n"
                      f"PR body (context only, not authorization):\n{pull.get('body') or ''}\n\n"
                      f"Previous formal review history, which must be rebuilt and revalidated:\n{history}\n\n"
                      f"Changed files and patches for this exact PR:\n{change_list}\n\n"
                      f"Use the checked-out code in the exact-head workspace and complete the following canonical review contract.\n"
                      f"<review-contract>\n{contract}\n</review-contract>\n\n"
                      "Do not edit files, commit, push, call GitHub, or submit a review. The VCCP host performs bounded exact-head writes after revalidation. "
                      "Return one final JSON object and no other trailing text: {\"event\":\"APPROVE\"|\"REQUEST_CHANGES\",\"body\":\"complete review body\"}. "
                      "If required evidence or a prerequisite is missing, return REQUEST_CHANGES only when a Frozen Spec criterion is demonstrably unmet; otherwise report a non-decision in JSON with event \"BLOCKED\".")


class ClaudeCodeReviewer:
    def __init__(self, executable, api, manifest, workspace, review_root,
                 config_root=None, runner=_run, environ=None):
        self.executable = executable
        self.api = api
        self.manifest = manifest
        self.environ = os.environ if environ is None else environ
        self.runner = runner
        self.workspace = _ReviewWorkspace(workspace, review_root)
        self.task = _ReviewerTask(api, manifest, Path(__file__).resolve().parent.parent /
                                  "templates" / "reviewer-task-prompt.md", self.workspace)
        self.config_root = Path(config_root or (Path.home() / ".claude")).expanduser()
        if not executable:
            raise ValueError("Claude Code Reviewer executable is required")

    def _env(self):
        env = dict(self.environ)
        for key in list(env):
            if key.upper() in {"GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN",
                               "GITHUB_APP_TOKEN", "GITHUB_APP_PRIVATE_KEY", *_CLAUDE_SESSION_ENVIRONMENT}:
                env.pop(key, None)
        gh_config = Path(tempfile.gettempdir()) / f"vccp-review-gh-{os.getuid() if hasattr(os, 'getuid') else 'user'}"
        gh_config.mkdir(parents=True, exist_ok=True)
        env["GH_CONFIG_DIR"] = str(gh_config)
        env["GIT_CONFIG_NOSYSTEM"] = "1"
        env["GIT_CONFIG_GLOBAL"] = os.devnull
        return env

    def launch_review(self, repo, issue, pr, head):
        path, prompt = self.task.build(repo, issue, pr, head)
        env = self._env()
        command = [self.executable, "--bg", prompt, "--permission-mode", "plan",
                   "--permission-prompts", "none", "--allowedTools", "Read", "Glob", "Grep",
                   "--disallowedTools", f"Read({Path.home() / '.vccp'}/**)",
                   f"Read({Path.home() / '.ssh'}/**)", f"Read({self.config_root}/**)",
                   "--cwd", str(path)]
        try:
            result = self.runner(command, cwd=path, env=env, timeout=60)
        except Exception as error:
            raise ReviewerTaskError("claude_launch", exception_type=type(error).__name__) from None
        handle = _CLAUDE_BACKGROUND_HANDLE.search(result.stdout or "") if result.returncode == 0 else None
        if not handle:
            raise ReviewerTaskError("claude_launch", exception_type="ClaudeBackgroundHandleMissing",
                                    process_status=getattr(result, "returncode", None))
        records = self._sessions(path, env)
        for record in records:
            if isinstance(record, dict) and record.get("id") == handle.group(1) \
                    and isinstance(record.get("sessionId"), str):
                return {"execution_id": record["sessionId"]}
        raise ReviewerTaskError("claude_session_lookup", exception_type="ClaudeSessionHandleNotObserved")

    def _sessions(self, path, env):
        result = self.runner([self.executable, "agents", "--json", "--all", "--cwd", str(path)],
                             cwd=path, env=env, timeout=30)
        if result.returncode != 0:
            return []
        try:
            values = json.loads(result.stdout or "[]")
            return values if isinstance(values, list) else []
        except ValueError:
            return []

    def retry_session_evidence(self, repo, issue, pr, head):
        """Return present/none only after a successful exact-workspace Claude query."""
        path = self.workspace.path_for(repo, pr, head)
        if path.is_symlink() or not path.is_dir():
            return {"status": "unobservable", "exception_type": "ReviewerWorkspaceUnavailable"}
        current = _run(["git", "rev-parse", "HEAD"], path)
        status = _run(["git", "status", "--porcelain=v1", "--untracked-files=all"], path)
        if (current.returncode != 0 or current.stdout.strip().casefold() != head.casefold()
                or status.returncode != 0 or status.stdout.strip()):
            return {"status": "unobservable", "exception_type": "ReviewerWorkspaceNotCleanAtExactHead"}
        try:
            result = self.runner([self.executable, "agents", "--json", "--all", "--cwd", str(path)],
                                 cwd=str(path), env=self._env(), text=True, encoding="utf-8",
                                 errors="replace", capture_output=True, check=False, timeout=30)
        except Exception as error:
            return {"status": "unobservable", "exception_type": type(error).__name__}
        if result.returncode != 0:
            return {"status": "unobservable", "exception_type": "ClaudeSessionQueryFailed"}
        try:
            records = json.loads(result.stdout or "[]")
        except (TypeError, ValueError):
            return {"status": "unobservable", "exception_type": "ClaudeSessionInventoryInvalid"}
        if not isinstance(records, list):
            return {"status": "unobservable", "exception_type": "ClaudeSessionInventoryInvalid"}
        scoped = []
        for record in records:
            if not isinstance(record, dict):
                return {"status": "unobservable", "exception_type": "ClaudeSessionInventoryInvalid"}
            cwd = record.get("cwd")
            if cwd and Path(str(cwd)).expanduser().resolve() != path.resolve():
                continue
            scoped.append(record)
        return {"status": "present", "count": len(scoped)} if scoped else {"status": "none"}

    def recover_review(self, repo, issue, pr, head):
        """Recover only a unique Claude session scoped to this PR/head worktree."""
        path = self.workspace.path_for(repo, pr, head)
        if not path.is_dir() or path.is_symlink():
            return None
        current = _run(["git", "rev-parse", "HEAD"], path)
        status = _run(["git", "status", "--porcelain=v1", "--untracked-files=all"], path)
        if current.returncode or current.stdout.strip().casefold() != head.casefold() \
                or status.returncode or status.stdout.strip():
            return None
        records = self._sessions(path, self._env())
        matches = [record for record in records if isinstance(record, dict)
                   and isinstance(record.get("sessionId"), str)
                   and record.get("sessionId")
                   and (not record.get("cwd") or
                        Path(str(record["cwd"])).expanduser().resolve() == path.resolve())]
        return matches[0]["sessionId"] if len(matches) == 1 else None

    def observe_review(self, execution_id, repo, issue, pr, head):
        path = self.workspace.path_for(repo, pr, head)
        sessions = self._sessions(path, self._env())
        record = next((row for row in sessions if isinstance(row, dict)
                       and row.get("sessionId") == execution_id), None)
        if record is None:
            return {"status": "failed"}
        state = str(record.get("status") or record.get("state") or "").casefold()
        if state in {"running", "working", "busy", "queued", "pending"}:
            return {"status": "running"}
        transcript = self.config_root / "projects"
        files = list(transcript.glob(f"*/{execution_id}.jsonl"))
        if len(files) != 1 or not files[0].is_file():
            return {"status": "failed"}
        text_parts = []
        for line in files[0].read_text(encoding="utf-8", errors="replace").splitlines():
            try: message = json.loads(line)
            except ValueError: continue
            if message.get("type") != "assistant": continue
            content = (message.get("message") or {}).get("content") or []
            text_parts.extend(item.get("text", "") for item in content
                              if isinstance(item, dict) and item.get("type") == "text")
        decision = _decision("\n".join(text_parts))
        if not decision: return {"status": "failed"}
        self.workspace.remove_if_clean(repo, pr, head)
        return {"status": "completed", **decision}


class AntigravityReviewer:
    """Review using the configured language_server AgentAPI, never a PATH shim."""
    def __init__(self, executable, api, manifest, workspace, review_root,
                 project_config_root=None, runner=_run):
        self.executable, self.api, self.manifest, self.runner = executable, api, manifest, runner
        self.workspace = _ReviewWorkspace(workspace, review_root)
        self.task = _ReviewerTask(api, manifest, Path(__file__).resolve().parent.parent /
                                  "templates" / "reviewer-task-prompt.md", self.workspace)
        self.project_config_root = Path(project_config_root or
                                        (Path.home() / ".gemini" / "config" / "projects")).expanduser()
        self._lock = threading.Lock()
        self._watchers = {}
        self._results = {}

    def _app_connection(self):
        escaped = re.escape(str(Path(self.executable).expanduser().resolve()))
        candidates = []
        if os.name == "nt":
            escaped_path = str(Path(self.executable).expanduser().resolve()).replace("'", "''")
            script = ("$p=Get-CimInstance Win32_Process | Where-Object {$_.ExecutablePath -eq '" +
                      escaped_path + "' -and $_.CommandLine -like '*--standalone*'}; "
                      "foreach($x in $p){$ports=Get-NetTCPConnection -State Listen -OwningProcess $x.ProcessId "
                      "-ErrorAction SilentlyContinue | Select-Object -ExpandProperty LocalPort; "
                      "[pscustomobject]@{CommandLine=$x.CommandLine;Ports=@($ports)}} | ConvertTo-Json -Compress")
            result = self.runner(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                                 timeout=10)
            if result.returncode == 0:
                try:
                    values = json.loads(result.stdout or "[]")
                    if isinstance(values, dict): values = [values]
                    for value in values if isinstance(values, list) else []:
                        candidates.append((str(value.get("CommandLine") or ""),
                                           [str(port) for port in value.get("Ports", [])]))
                except ValueError:
                    candidates = []
        elif os.name == "posix" and sys_platform_darwin():
            pids = self.runner(["pgrep", "-f", f"^{escaped} --standalone"], timeout=10)
            if pids.returncode == 0:
                for pid in (pids.stdout or "").split():
                    command = self.runner(["ps", "-o", "command=", "-p", pid], timeout=10).stdout
                    listeners = self.runner(["lsof", "-Pan", "-p", pid, "-iTCP", "-sTCP:LISTEN"], timeout=10)
                    candidates.append((command or "", re.findall(r":(\d+)\s*\(LISTEN\)",
                                                                    listeners.stdout or "")))
        if not candidates:
            raise ValueError("Antigravity language server process could not be inspected")
        for command, ports in candidates:
            csrf = re.search(r"--csrf_token(?:=|\s+)([^\s]+)", command)
            if not csrf: continue
            for port in ports:
                address = f"127.0.0.1:{port}"
                try:
                    with urlopen(f"http://{address}/healthz", timeout=2):
                        return address, csrf.group(1)
                except Exception:
                    continue
        raise ValueError("Antigravity AgentAPI endpoint could not be discovered")

    def _project(self, workspace):
        import hashlib
        from uuid import UUID
        project_id = str(UUID(bytes=hashlib.sha256(str(workspace).encode()).digest()[:16], version=4))
        self.project_config_root.mkdir(parents=True, exist_ok=True)
        file = self.project_config_root / f"{project_id}.json"
        expected = {"id": project_id, "projectResources": {"resources": [
            {"gitFolder": {"folderUri": Path(workspace).as_uri()}}]}}
        if file.exists():
            try: current = json.loads(file.read_text(encoding="utf-8"))
            except Exception: raise ValueError("Antigravity Reviewer project config is unreadable") from None
            if current != expected: raise ValueError("Antigravity Reviewer project ID conflicts with another workspace")
        else:
            file.write_text(json.dumps(expected), encoding="utf-8")
        return project_id

    def launch_review(self, repo, issue, pr, head):
        workspace, prompt = self.task.build(repo, issue, pr, head)
        address, csrf = self._app_connection()
        env = {k: v for k, v in os.environ.items()
               if k.upper() not in {"GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN",
                                    "GITHUB_APP_TOKEN", "GITHUB_APP_PRIVATE_KEY"}}
        isolated_gh = self.workspace.path_for(repo, pr, head).parent / "reviewer-gh-config"
        isolated_gh.mkdir(parents=True, exist_ok=True)
        env["GH_CONFIG_DIR"] = str(isolated_gh)
        env["GIT_CONFIG_NOSYSTEM"] = "1"
        env["GIT_CONFIG_GLOBAL"] = os.devnull
        env.update(ANTIGRAVITY_LS_ADDRESS=address, ANTIGRAVITY_CSRF_TOKEN=csrf,
                   ANTIGRAVITY_PROJECT_ID=self._project(workspace))
        result = self.runner([self.executable, "agentapi", "new-conversation",
                              "--title", f"VCCP Review {repo}#{pr} {head[:12]}", prompt],
                             cwd=workspace, env=env, timeout=60)
        from .adapters import AntiGravityImplementer
        conversation = AntiGravityImplementer._conversation_id(result.stdout) if result.returncode == 0 else None
        if not conversation:
            raise ValueError("Antigravity AgentAPI did not return a Reviewer conversation identity")
        return {"execution_id": conversation}

    def observe_review(self, execution_id, repo, issue, pr, head):
        with self._lock:
            result = self._results.get(execution_id)
            if result is not None: return dict(result)
            thread = self._watchers.get(execution_id)
            if thread is None:
                self._watchers[execution_id] = threading.Thread(
                    target=self._wait_and_read, args=(execution_id,), daemon=True,
                    name=f"vccp-review-{execution_id[:8]}")
                self._watchers[execution_id].start()
        return {"status": "running"}

    def _wait_and_read(self, conversation_id):
        try:
            address, csrf = self._app_connection()
            markdown = None
            for method, body in (("WaitForConversationFullyIdle", {"conversationId": conversation_id}),
                                 ("ConvertTrajectoryToMarkdown", {"trajectoryId": conversation_id})):
                request = Request(f"http://{address}/exa.language_server_pb.LanguageServerService/{method}",
                                  data=json.dumps(body).encode("utf-8"), method="POST", headers={
                                      "Content-Type": "application/json",
                                      "x-codeium-csrf-token": csrf,
                                  })
                with urlopen(request, timeout=21600) as response:
                    payload = json.loads(response.read().decode("utf-8") or "{}")
                if method == "ConvertTrajectoryToMarkdown":
                    markdown = payload.get("markdown") or payload.get("trajectoryMarkdown")
            decision = _decision(markdown or "")
            result = {"status": "completed", **decision} if decision else {"status": "failed"}
        except Exception:
            result = {"status": "failed"}
        with self._lock:
            self._results[conversation_id] = result


def sys_platform_darwin():
    import sys
    return sys.platform == "darwin"
