"""Repo-fenced GitHub App git-push backend."""
from __future__ import annotations

import base64
import os
from pathlib import Path
import re
import subprocess
from typing import Callable
from urllib.parse import quote, urlparse

from .github_app import (GitHubAppCredentialProvider, GitHubAppError,
                         GitHubAppIdentityError, repository_from_remote)


_SHA = re.compile(r"^[0-9a-fA-F]{40}$")


class GitHubAppPushBackend:
    """Push one verified local branch to its enrolled repository and verify the remote SHA."""

    def __init__(self, credential_provider: GitHubAppCredentialProvider, workspace: str | Path,
                 target_repository: str, base_branch: str, remote_name: str = "origin",
                 git_executable: str = "git", runner: Callable = subprocess.run):
        if not credential_provider or not workspace or not target_repository or not base_branch:
            raise ValueError("controlled push backend configuration is incomplete")
        self.provider = credential_provider
        self.workspace = Path(workspace).expanduser().resolve()
        self.target_repository = target_repository.casefold()
        self.base_branch, self.remote_name = base_branch, remote_name
        self.git_executable, self.runner = git_executable, runner

    def _run_git(self, args: list[str], env=None):
        try:
            result = self.runner([self.git_executable, *args], cwd=str(self.workspace), env=env,
                                 text=True, capture_output=True, check=False, timeout=60)
        except Exception:
            raise GitHubAppError("controlled git operation failed") from None
        if result.returncode != 0:
            raise GitHubAppError("controlled git operation failed")
        return result.stdout.strip()

    def _verify_workspace_and_remote(self, repo: str):
        if repo.casefold() != self.target_repository or repo.casefold() != self.provider.target_repository:
            raise GitHubAppIdentityError("controlled push target repository is not authorized")
        toplevel = Path(self._run_git(["rev-parse", "--show-toplevel"])).resolve()
        if os.path.normcase(str(toplevel)) != os.path.normcase(str(self.workspace)):
            raise GitHubAppIdentityError("controlled push workspace does not match configured checkout")
        remote = self._run_git(["remote", "get-url", self.remote_name])
        if (repository_from_remote(remote, self.provider.api_url) or "").casefold() != self.target_repository:
            raise GitHubAppIdentityError("configured Git remote does not match consumer repository")
        pushurl_result = self.runner([self.git_executable, "remote", "get-url", "--push", self.remote_name],
                                     cwd=str(self.workspace), text=True, capture_output=True,
                                     check=False, timeout=30)
        if pushurl_result.returncode == 0 and (repository_from_remote(
                pushurl_result.stdout.strip(), self.provider.api_url) or "").casefold() != self.target_repository:
            raise GitHubAppIdentityError("configured Git push remote does not match consumer repository")
        host = urlparse(self.provider.api_url).hostname
        if host and host.casefold() == "api.github.com":
            host = "github.com"
        return f"https://{host}/{repo}.git"

    @staticmethod
    def _git_auth_environment(environment: dict, push_url: str, token: str, remote_name: str):
        result = dict(environment)
        try:
            count = int(result.get("GIT_CONFIG_COUNT", "0"))
        except ValueError:
            raise GitHubAppError("inherited Git configuration is invalid") from None
        host = urlparse(push_url).hostname
        header = "Basic " + base64.b64encode(f"x-access-token:{token}".encode("utf-8")).decode("ascii")
        additions = {
            f"remote.{remote_name}.pushurl": push_url,
            f"http.https://{host}/.extraheader": f"AUTHORIZATION: {header}",
        }
        result["GIT_CONFIG_COUNT"] = str(count + len(additions))
        for offset, (key, value) in enumerate(additions.items(), count):
            result[f"GIT_CONFIG_KEY_{offset}"] = key
            result[f"GIT_CONFIG_VALUE_{offset}"] = value
        return result

    def push(self, repo: str, source_ref: str, expected_sha: str, destination_ref: str,
             controlled: bool = False):
        if not controlled:
            raise GitHubAppIdentityError("push is available only through the runtime-controlled wrapper")
        if not _SHA.fullmatch(expected_sha or ""):
            raise GitHubAppIdentityError("controlled push requires a full expected SHA")
        push_url = self._verify_workspace_and_remote(repo)
        source_name = source_ref.removeprefix("refs/heads/")
        if not source_name or source_name.startswith("-"):
            raise GitHubAppIdentityError("controlled push source ref is invalid")
        destination = destination_ref if destination_ref.startswith("refs/heads/") else f"refs/heads/{destination_ref}"
        destination_branch = destination.removeprefix("refs/heads/")
        if not destination_branch or destination_branch.startswith("-") or destination_branch == self.base_branch:
            raise GitHubAppIdentityError("controlled push destination ref is invalid")
        self._run_git(["check-ref-format", "--branch", source_name])
        self._run_git(["check-ref-format", destination])
        actual = self._run_git(["rev-parse", "--verify", f"refs/heads/{source_name}^{{commit}}"]).lower()
        if actual != expected_sha.lower():
            raise GitHubAppIdentityError("local source ref does not match expected SHA")

        token = self.provider.get_token(repo)
        environment = self._git_auth_environment(os.environ.copy(), push_url, token, self.remote_name)
        environment["VCCP_CONTROLLED_PUSH"] = "1"
        self._run_git(["push", "--porcelain", self.remote_name,
                       f"{expected_sha.lower()}:{destination}"], env=environment)

        owner, name = repo.split("/", 1)
        path = f"/repos/{owner}/{name}/git/ref/heads/{quote(destination_branch, safe='/')}"
        status, reference = self.provider.request("GET", path, repo)
        remote_sha = str((reference.get("object") or {}).get("sha", ""))
        if status != 200 or remote_sha.casefold() != expected_sha.casefold():
            raise GitHubAppError("remote ref verification failed after controlled push")
        return {"status": "pushed", "repository": repo, "destination_ref": destination,
                "sha": remote_sha}
