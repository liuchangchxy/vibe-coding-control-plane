"""Machine-local configuration qualification for the existing Generic Runtime."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

from .adapters import build_runtime
from .core import RuntimeConfig

_REQUIRED = (
    "database_path", "workspace", "owner_id", "antigravity_executable",
    "app_gh_executable", "app_git_push_executable", "github_read_token_env",
)
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class _ReadOnlyQualificationAPI:
    """Construction-only API: readiness never performs GitHub reads or writes."""


class _ReadOnlyQualificationWriter:
    """Construction-only writer: readiness never changes coordination state."""


def _remote_repository(workspace: Path, host: str) -> str | None:
    result = subprocess.run(
        ["git", "-C", str(workspace), "remote", "get-url", "origin"],
        text=True, capture_output=True, check=False,
    )
    if result.returncode:
        return None
    match = re.search(rf"(?:{re.escape(host)}[:/])([^/]+/[^/]+?)(?:\.git)?$", result.stdout.strip(), re.IGNORECASE)
    return match.group(1).casefold() if match else None


def qualify_activation(manifest: dict, local_config: dict, repo: str, environ=None, host="github.com") -> dict:
    """Return fail-closed activation evidence while constructing the real adapter graph on scratch SQLite."""
    env = os.environ if environ is None else environ
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

    workspace = Path(local_config["workspace"]).expanduser()
    if not workspace.is_dir():
        blockers.append("workspace directory is unavailable")
    elif _remote_repository(workspace, host) != repo.casefold():
        blockers.append("workspace origin does not match the consumer repository")

    for key in ("antigravity_executable", "app_gh_executable", "app_git_push_executable"):
        executable = Path(local_config[key]).expanduser()
        if not executable.is_file():
            blockers.append(f"required executable is unavailable: {key}")
    if Path(local_config["app_gh_executable"]).name.casefold() in {"gh", "gh.exe"}:
        blockers.append("app_gh_executable must identify the controlled GitHub writer, not ordinary gh")

    database_path = Path(local_config["database_path"]).expanduser()
    database_parent = database_path.parent
    if not database_parent.is_dir() or not os.access(database_parent, os.W_OK):
        blockers.append("database_path parent must be an existing writable directory")

    token_name = local_config["github_read_token_env"]
    if not _ENV_NAME.fullmatch(token_name):
        blockers.append("github_read_token_env must be an environment-variable name")
    elif not env.get(token_name):
        blockers.append(f"credential source unavailable: environment variable {token_name} (present: false)")

    if blockers:
        return {"status": "not_ready", "blockers": blockers}

    try:
        # _Store initializes schema during construction. Use the configured DB's parent
        # to qualify the same production graph without modifying the production database.
        with tempfile.TemporaryDirectory(prefix="vccp-readiness-", dir=database_parent) as scratch:
            scratch_config = dict(local_config, database_path=str(Path(scratch) / "qualification.sqlite"))
            runtime = build_runtime(manifest, scratch_config,
                                    api=_ReadOnlyQualificationAPI(),
                                    writer=_ReadOnlyQualificationWriter())
            if runtime.core is None or runtime.workflow is None or runtime.implementer is None or runtime.lifecycle is None:
                raise ValueError("runtime component construction was incomplete")
    except (OSError, TypeError, ValueError) as error:
        return {"status": "not_ready", "blockers": [f"production runtime construction failed: {error}"]}
    return {"status": "ready", "blockers": [], "credential_source": {"environment_variable": token_name, "present": True}}


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
