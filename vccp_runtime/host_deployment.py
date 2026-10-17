"""Native single-user host reconciliation for the production Runner."""
from __future__ import annotations

import os
from pathlib import Path
import plistlib
import re
import subprocess
import tempfile


def _run(command, runner, **kwargs):
    return runner(command, text=True, capture_output=True, check=False, timeout=20, **kwargs)


def _loaded(label, user_id, runner):
    result = _run(["launchctl", "print", f"gui/{user_id}/{label}"], runner)
    return result.returncode == 0, result.stdout or ""


def _valid_existing_job(plist_path, label, repo):
    try:
        with open(plist_path, "rb") as stream:
            value = plistlib.load(stream)
    except (OSError, plistlib.InvalidFileException, ValueError):
        return False
    args = value.get("ProgramArguments")
    return (value.get("Label") == label and isinstance(args, list)
            and "-m" in args and args[args.index("-m") + 1:args.index("-m") + 2] == ["vccp_runtime.runner"]
            and "--repo" in args and args[args.index("--repo") + 1:args.index("--repo") + 2] == [repo])


def reconcile_macos_launch_agent(*, repo: str, runtime_root: str, manifest_path: str,
                                 runtime_config_path: str, database_path: str,
                                 python_executable: str, label: str, plist_path: str,
                                 user_id: str, readiness, runner=subprocess.run,
                                 environ=None) -> dict:
    """Stop, reconcile, qualify, start, and verify one exact-revision LaunchAgent."""
    runtime = Path(runtime_root).expanduser().resolve()
    destination = Path(plist_path).expanduser()
    if not re.fullmatch(r"[^/]+/[^/]+", repo) or not runtime.is_dir():
        return {"status": "blocked", "reason": "Consumer identity or exact runtime checkout is unavailable"}
    loaded, _ = _loaded(label, user_id, runner)
    if loaded:
        if not destination.is_file() or not _valid_existing_job(destination, label, repo):
            return {"status": "blocked", "reason": "loaded LaunchAgent is not the configured Consumer Runner"}
        stopped = _run(["launchctl", "bootout", f"gui/{user_id}/{label}"], runner)
        if stopped.returncode != 0:
            return {"status": "blocked", "reason": "existing Consumer Runner could not be stopped"}
        still_loaded, _ = _loaded(label, user_id, runner)
        if still_loaded:
            return {"status": "blocked", "reason": "existing Consumer Runner still owns the LaunchAgent"}

    old = {}
    if destination.exists():
        if not _valid_existing_job(destination, label, repo):
            return {"status": "blocked", "reason": "existing LaunchAgent file belongs to another job"}
        with destination.open("rb") as stream:
            old = plistlib.load(stream)
    environment = dict(environ if environ is not None else os.environ)
    for key in list(environment):
        if key.upper() in {"GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN",
                           "GITHUB_APP_TOKEN", "GITHUB_APP_PRIVATE_KEY"}:
            environment.pop(key, None)
    environment["PYTHONPATH"] = str(runtime)
    environment["PYTHONUNBUFFERED"] = "1"
    program = [python_executable, "-u", "-m", "vccp_runtime.runner",
               "--manifest", str(Path(manifest_path).expanduser().resolve()),
               "--runtime-config", str(Path(runtime_config_path).expanduser().resolve()),
               "--repo", repo, "--continuous", "--poll-interval", "30"]
    definition = {
        "Label": label,
        "ProgramArguments": program,
        "WorkingDirectory": str(runtime),
        "EnvironmentVariables": environment,
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 10,
        "StandardOutPath": old.get("StandardOutPath", str(destination.parent / f"{label}.out.log")),
        "StandardErrorPath": old.get("StandardErrorPath", str(destination.parent / f"{label}.err.log")),
        "ProcessType": "Standard",
    }
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + f".{os.getpid()}.tmp")
    try:
        with temporary.open("xb") as stream:
            plistlib.dump(definition, stream, sort_keys=True)
        os.chmod(temporary, 0o644)
        os.replace(temporary, destination)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass

    qualified = readiness()
    if not isinstance(qualified, dict) or qualified.get("status") != "ready":
        return {"status": "blocked", "reason": "machine readiness did not pass", "readiness": qualified}
    bootstrap = _run(["launchctl", "bootstrap", f"gui/{user_id}", str(destination)], runner)
    if bootstrap.returncode != 0:
        return {"status": "blocked", "reason": "LaunchAgent could not be installed"}
    started = _run(["launchctl", "kickstart", "-k", f"gui/{user_id}/{label}"], runner)
    if started.returncode != 0:
        return {"status": "blocked", "reason": "Consumer Runner could not be started"}
    active, detail = _loaded(label, user_id, runner)
    match = re.search(r"\bpid\s*=\s*(\d+)", detail)
    pid = int(match.group(1)) if match else None
    if not active or not pid:
        return {"status": "blocked", "reason": "LaunchAgent did not produce one running process"}
    process = _run(["ps", "-p", str(pid), "-o", "command="], runner)
    if process.returncode != 0 or "vccp_runtime.runner" not in (process.stdout or ""):
        return {"status": "blocked", "reason": "LaunchAgent process does not match the VCCP Runner"}
    return {"status": "running", "mechanism": "launchd", "label": label,
            "pid": pid, "revision": runtime.name, "runtime_root": str(runtime)}
