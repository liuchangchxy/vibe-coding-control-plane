"""Machine-local configuration qualification for the existing Generic Runtime."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

from .adapters import (GitHubAPI, GitHubAppWriter, _make_credential_provider,
                       _workspace_repository, build_runtime)
from .core import RuntimeConfig
from .github_app import (DPAPIFileCredentialSource, GitHubAppError,
                         GitHubAppNotInstalled, GitHubAppPermissionError)
from .github_push import GitHubAppPushBackend
from .runner import RuntimeAlreadyRunning, RuntimeLock

_REQUIRED = (
    "database_path", "workspace", "owner_id", "antigravity_executable",
)


def _remote_repository(workspace: Path, host: str) -> str | None:
    result = subprocess.run(
        ["git", "-C", str(workspace), "remote", "get-url", "origin"],
        text=True, capture_output=True, check=False,
    )
    if result.returncode:
        return None
    match = re.search(rf"(?:{re.escape(host)}[:/])([^/]+/[^/]+?)(?:\.git)?$", result.stdout.strip(), re.IGNORECASE)
    return match.group(1).casefold() if match else None


def qualify_activation(manifest: dict, local_config: dict, repo: str, environ=None, host="github.com",
                       credential_provider=None, api=None, writer=None, push_backend=None) -> dict:
    """Read-only production qualification: verify App access, read target repo, and construct the graph."""
    blockers: list[str] = []
    try:
        RuntimeConfig.from_manifest(manifest)
        if not isinstance(manifest.get("repository"), dict) or not manifest["repository"].get("base_branch"):
            raise ValueError("repository.base_branch is required")
        if not isinstance(manifest["repository"].get("implementer_authors"), list):
            raise ValueError("repository.implementer_authors is required")
        if not isinstance(manifest.get("reviewer"), dict) or not isinstance(manifest["reviewer"].get("required_checks"), list):
            raise ValueError("reviewer.required_checks is required")
    except (AttributeError, KeyError, TypeError, ValueError) as error:
        blockers.append(f"repository contract: {error}")

    if not isinstance(local_config, dict):
        return {"status": "not_ready", "blockers": ["local configuration must be a JSON object"]}

    missing = [key for key in _REQUIRED if not isinstance(local_config.get(key), str) or not local_config[key].strip()]
    if missing:
        blockers.append("local configuration missing: " + ", ".join(missing))
        return {"status": "not_ready", "blockers": blockers}

    target_repo = local_config.get("target_repository")
    enrollment = local_config.get("enrollment")
    if not isinstance(target_repo, str) or target_repo.casefold() != repo.casefold():
        blockers.append("machine-local target_repository does not match the requested consumer repository")
    if not isinstance(enrollment, dict) or not isinstance(enrollment.get("authorized_repository"), str) or enrollment["authorized_repository"].casefold() != repo.casefold():
        blockers.append(f"human enrollment authorization is required for {repo}")

    workspace = Path(local_config["workspace"]).expanduser()
    if not workspace.is_dir():
        blockers.append("workspace directory is unavailable")
    elif _remote_repository(workspace, host) != repo.casefold():
        blockers.append("workspace origin does not match the consumer repository")

    for key in ("antigravity_executable",):
        executable = Path(local_config[key]).expanduser()
        if not executable.is_file():
            blockers.append(f"required executable is unavailable: {key}")

    database_path = Path(local_config["database_path"]).expanduser()
    database_parent = database_path.parent
    if not database_parent.is_dir() or not os.access(database_parent, os.W_OK):
        blockers.append("database_path parent must be an existing writable directory")

    if not isinstance(local_config.get("conversation_roots", []), list):
        blockers.append("conversation_roots must be a list of machine-local directories")
    elif any(not Path(root).expanduser().is_dir() for root in local_config.get("conversation_roots", [])):
        blockers.append("configured AntiGravity conversation root is unavailable")

    app_config = local_config.get("github_app")
    if not isinstance(app_config, dict) or not str(app_config.get("app_id", "")).isdigit():
        blockers.append("machine-local github_app.app_id is required")
        app_config = {}
    credential_source = app_config.get("credential_source") or {}
    if credential_source.get("type") != "windows_dpapi_file" or not credential_source.get("path"):
        blockers.append("github_app.credential_source must identify a Windows DPAPI file")
    else:
        source_path = Path(credential_source["path"]).expanduser()
        if not source_path.is_file():
            blockers.append("protected GitHub App credential source is unavailable")
        else:
            try:
                DPAPIFileCredentialSource(source_path).load_private_key()
            except GitHubAppError as error:
                blockers.append(str(error))

    if blockers:
        return {"status": "not_ready", "blockers": blockers}

    try:
        api_url = app_config.get("api_url", "https://api.github.com")
        target_repo = _workspace_repository(str(workspace), api_url)
        if target_repo.casefold() != repo.casefold():
            return {"status": "not_ready", "blockers": ["workspace origin does not match the consumer repository"]}
        credential_provider = credential_provider or _make_credential_provider(local_config, target_repo)
        installation = credential_provider.probe(target_repo)
        if not installation.get("installed"):
            return {"status": "not_ready", "blockers": [
                f"GitHub App is not installed on {repo}; a human must install it"
            ], "installation": {"installed": False}}
        if not installation.get("permissions_sufficient"):
            missing = installation.get("missing_permissions") or {}
            report = ", ".join(f"{name}:{level}" for name, level in sorted(missing.items()))
            return {"status": "not_ready", "blockers": [
                f"GitHub App installation needs human-approved permissions: {report}"
            ], "installation": {"installed": True, "permissions_sufficient": False,
                                "missing_permissions": missing}}
        api = api or GitHubAPI.with_app_provider(credential_provider)
        repository = api.get(f"/repos/{target_repo}")
        if str(repository.get("full_name", "")).casefold() != target_repo.casefold():
            return {"status": "not_ready", "blockers": ["GitHub App repository read returned the wrong target"]}
        issue_contract = manifest["issue_contract"]
        writer = writer or GitHubAppWriter(
            credential_provider=credential_provider, target_repository=target_repo,
            allowed_labels=(issue_contract["active_coordination_labels"]
                            + issue_contract["terminal_coordination_labels"]),
        )
        push_backend = push_backend or GitHubAppPushBackend(
            credential_provider, str(workspace), target_repo, manifest["repository"]["base_branch"],
        )
        # _Store initializes schema during construction. Use the configured DB's parent
        # to qualify the production graph without modifying the production database.
        with tempfile.TemporaryDirectory(prefix="vccp-readiness-", dir=database_parent) as scratch:
            scratch_config = dict(local_config, database_path=str(Path(scratch) / "qualification.sqlite"))
            runtime = build_runtime(manifest, scratch_config,
                                    api=api, writer=writer, credential_provider=credential_provider,
                                    push_backend=push_backend)
            if runtime.core is None or runtime.workflow is None or runtime.implementer is None or runtime.lifecycle is None:
                raise ValueError("runtime component construction was incomplete")
        with RuntimeLock(repo, str(database_path)):
            pass
    except GitHubAppNotInstalled as error:
        return {"status": "not_ready", "blockers": [str(error)], "installation": {"installed": False}}
    except GitHubAppPermissionError as error:
        return {"status": "not_ready", "blockers": [str(error)],
                "installation": {"installed": True, "permissions_sufficient": False,
                                 "missing_permissions": error.missing_permissions}}
    except GitHubAppError as error:
        return {"status": "not_ready", "blockers": [str(error)]}
    except (OSError, RuntimeAlreadyRunning, TypeError, ValueError) as error:
        return {"status": "not_ready", "blockers": [f"production runtime qualification failed: {error}"]}
    return {"status": "ready", "blockers": [],
            "credential_source": {"type": "windows_dpapi_file", "present": True},
            "installation": {"installed": True, "permissions_sufficient": True,
                             "missing_permissions": []},
            "repository_read": "passed", "writer_constructed": True,
            "push_backend_constructed": True}


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        result = qualify_activation(payload["manifest"], payload["local_config"], payload["repo"], host=payload.get("host", "github.com"))
    except (json.JSONDecodeError, KeyError, TypeError) as error:
        result = {"status": "not_ready", "blockers": [f"invalid readiness input: {type(error).__name__}"]}
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["status"] == "ready" else 2


if __name__ == "__main__":
    raise SystemExit(main())
