"""Machine-local configuration qualification for the existing Generic Runtime."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

from .adapters import (GitHubAPI, GitHubAppWriter, _PROVIDER_ADAPTERS, _configured_providers_for_role,
                       _discover_antigravity_agentapi_environment, _make_credential_provider,
                       _workspace_repository, build_runtime)
from .core import RuntimeConfig
from .github_app import (DPAPIFileCredentialSource, GitHubAppError,
                         GitHubAppNotInstalled, GitHubAppPermissionError,
                         POSIXFileCredentialSource, make_credential_source)
from .github_push import GitHubAppPushBackend
from .runner import RuntimeAlreadyRunning, RuntimeLock
from .providers import CLAUDE_AGENT_SDK_PROVIDER

_REQUIRED = (
    "database_path", "workspace", "owner_id",
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
                       credential_provider=None, api=None, writer=None, push_backend=None,
                       check_runtime_lock=True) -> dict:
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

    try:
        providers = _configured_providers_for_role(local_config, "implementer")
    except ValueError as error:
        providers = []
        blockers.append(f"machine-local providers: {error}")
    for settings in providers:
        provider_type = settings.get("type")
        if provider_type not in _PROVIDER_ADAPTERS:
            blockers.append(f"unsupported implementer provider type: {provider_type}")
            continue
        executable = settings.get("executable")
        if not isinstance(executable, str) or not executable.strip():
            blockers.append(f"{provider_type} provider requires a configured executable")
        elif not Path(executable).expanduser().is_file():
            blockers.append(f"required executable is unavailable: {provider_type}")
        elif provider_type == "antigravity":
            # An AntiGravity launch reaches AgentAPI through a CSRF-protected loopback
            # listener. Without that context a launch can only return UNKNOWN, so it is
            # machine readiness, not a launch-time surprise.
            environment = os.environ if environ is None else environ
            if not (environment.get("ANTIGRAVITY_LS_ADDRESS") and environment.get("ANTIGRAVITY_CSRF_TOKEN")) \
                    and _discover_antigravity_agentapi_environment(executable) is None:
                blockers.append("AntiGravity AgentAPI endpoint is not discoverable: "
                                "start AntiGravity or set ANTIGRAVITY_LS_ADDRESS and ANTIGRAVITY_CSRF_TOKEN")

    try:
        reviewer_settings = _configured_providers_for_role(local_config, "reviewer")
    except ValueError as error:
        reviewer_settings = []
        blockers.append(f"machine-local Reviewer provider: {error}")
    if reviewer_settings:
        identity = local_config.get("reviewer_github_identity")
        if not isinstance(identity, str) or not identity.strip():
            blockers.append("reviewer_github_identity is required for a local Reviewer binding")
        for settings in reviewer_settings:
            provider_type, executable = settings.get("type"), settings.get("executable")
            if provider_type == "claude_agent_sdk":
                if settings.get("key") != CLAUDE_AGENT_SDK_PROVIDER:
                    blockers.append("Claude Agent SDK Reviewer key must be claude_agent_sdk")
                try:
                    from importlib.metadata import version
                    version("claude-agent-sdk")
                    from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query
                    if ClaudeAgentOptions is None or ResultMessage is None or query is None:
                        raise ImportError("required Claude Agent SDK symbols are unavailable")
                except Exception:
                    blockers.append("Claude Agent SDK package is unavailable")
            elif provider_type == "antigravity":
                if not isinstance(executable, str) or not Path(executable).expanduser().is_file():
                    blockers.append(f"required Reviewer executable is unavailable: {provider_type}")
            else:
                blockers.append(f"unsupported local Reviewer provider type: {provider_type}")
        project_root = local_config.get("project_config_root")
        if any(item.get("type") == "antigravity" for item in reviewer_settings) \
                and project_root and not Path(project_root).expanduser().is_dir():
            blockers.append("configured AntiGravity project config root is unavailable")

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
    source_type = credential_source.get("type")
    if source_type not in ("windows_dpapi_file", "posix_file") or not credential_source.get("path"):
        blockers.append("github_app.credential_source must identify a Windows DPAPI file or POSIX file")
    else:
        source_path = Path(credential_source["path"]).expanduser()
        if not source_path.is_file():
            blockers.append("protected GitHub App credential source is unavailable")
        else:
            try:
                if source_type == "windows_dpapi_file":
                    DPAPIFileCredentialSource(source_path).load_private_key()
                else:
                    POSIXFileCredentialSource(source_path).load_private_key()
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
            if runtime.core is None or runtime.workflow is None or runtime.router is None or runtime.lifecycle is None:
                raise ValueError("runtime component construction was incomplete")
        if check_runtime_lock:
            with RuntimeLock(repo, str(database_path)):
                pass
        if reviewer_settings:
            from .local_reviewer import GitHubUserReviewerProvider
            reviewer_identity = local_config["reviewer_github_identity"]
            identity_status = GitHubUserReviewerProvider(
                target_repo, reviewer_identity).verify_identity()
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
            "credential_source": {"type": source_type, "present": True},
            "installation": {"installed": True, "permissions_sufficient": True,
                             "missing_permissions": []},
            "repository_read": "passed", "writer_constructed": True,
            "push_backend_constructed": True,
            "reviewer_identity": ({"login": identity_status["login"], "verified": True}
                                  if reviewer_settings else None)}


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
