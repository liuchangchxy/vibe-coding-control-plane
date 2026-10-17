"""Native single-user host reconciliation for the production Runner."""
from __future__ import annotations

import os
import hashlib
from contextlib import closing
from pathlib import Path
import plistlib
import re
import subprocess
import sqlite3
import time

from .runner import RuntimeLock, RuntimeAlreadyRunning


def _run(command, runner, **kwargs):
    return runner(command, text=True, capture_output=True, check=False, timeout=20, **kwargs)


def _loaded(label, user_id, runner):
    result = _run(["launchctl", "print", f"gui/{user_id}/{label}"], runner)
    return result.returncode == 0, f"{getattr(result, 'stdout', '') or ''}\n{getattr(result, 'stderr', '') or ''}"


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
        with destination.open("rb") as stream:
            current = plistlib.load(stream)
        expected_args = [python_executable, "-u", "-m", "vccp_runtime.runner", "--manifest",
                         str(Path(manifest_path).expanduser().resolve()), "--runtime-config",
                         str(Path(runtime_config_path).expanduser().resolve()), "--repo", repo,
                         "--continuous", "--poll-interval", "30"]
        manifest_digest = hashlib.sha256(Path(manifest_path).expanduser().read_bytes()).hexdigest()
        config_digest = hashlib.sha256(Path(runtime_config_path).expanduser().read_bytes()).hexdigest()
        if (current.get("WorkingDirectory") == str(runtime)
                and current.get("ProgramArguments") == expected_args
                and current.get("EnvironmentVariables", {}).get("PYTHONPATH") == str(runtime)
                and current.get("VCCPManifestSHA256") == manifest_digest
                and current.get("VCCPRuntimeConfigSHA256") == config_digest):
            active, detail = _loaded(label, user_id, runner)
            match = re.search(r"\bpid\s*=\s*(\d+)", detail)
            pid = int(match.group(1)) if match else None
            if active and pid:
                process = _run(["ps", "-p", str(pid), "-o", "command="], runner)
                if process.returncode == 0 and "vccp_runtime.runner" in (process.stdout or ""):
                    return {"status": "running", "no_op": True, "mechanism": "launchd",
                            "label": label, "pid": pid, "revision": runtime.name,
                            "runtime_root": str(runtime)}
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
        "VCCPManifestSHA256": hashlib.sha256(Path(manifest_path).expanduser().read_bytes()).hexdigest(),
        "VCCPRuntimeConfigSHA256": hashlib.sha256(Path(runtime_config_path).expanduser().read_bytes()).hexdigest(),
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


def _active_work(repo, database_path):
    database = Path(database_path).expanduser()
    if database.exists() and (database.is_symlink() or not database.is_file()):
        return "runtime database path is not a regular file"
    if not database.is_file():
        return None
    try:
        uri = f"{database.resolve().as_uri()}?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, timeout=1)) as connection:
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "attempts" in tables:
                rows = connection.execute("SELECT phase, closeout_pending FROM attempts WHERE lower(repo)=lower(?)", (repo,)).fetchall()
                terminal = {"CLAIM_FAILED", "LAUNCH_NOT_STARTED", "BUDGET_EXHAUSTED", "MERGED_SUCCESS",
                            "STOPPED_CANCELLED", "TERMINAL_UNRESOLVED", "COMPLETED", "FAILED"}
                active = [row[0] for row in rows if row[0] not in terminal or row[1]]
                if active:
                    return f"active Consumer execution exists ({', '.join(sorted(set(active)))})"
            if "controlled_pushes" in tables:
                pending = connection.execute("SELECT 1 FROM controlled_pushes WHERE lower(repo)=lower(?) AND phase='intent' LIMIT 1", (repo,)).fetchone()
                if pending:
                    return "active controlled push is awaiting durable closeout"
            if "reviewer_executions" in tables:
                states = connection.execute("SELECT state FROM reviewer_executions WHERE lower(repo)=lower(?)", (repo,)).fetchall()
                active = [row[0] for row in states if row[0] in {"STARTING", "RUNNING"}]
                if active:
                    return "active Local Reviewer execution exists"
    except (sqlite3.Error, OSError) as error:
        return f"runtime database could not be safely inspected: {error}"
    return None


def _inspect_remove_job(label, user_id, runner):
    result = _run(["launchctl", "print", f"gui/{user_id}/{label}"], runner)
    detail = f"{getattr(result, 'stdout', '') or ''}\n{getattr(result, 'stderr', '') or ''}"
    if result.returncode == 0:
        return True, detail, None
    lowered = detail.casefold()
    if any(phrase in lowered for phrase in ("could not find service", "no such process", "service not found")):
        return False, detail, None
    return False, detail, "launchd state could not be confirmed"


def remove_macos_launch_agent(*, repo: str, database_path: str, runtime_config_path: str,
                              label: str, plist_path: str,
                              user_id: str, runner=subprocess.run) -> dict:
    """Stop one proven Consumer job, verify its process/lock released, then remove its plist."""
    problem = _active_work(repo, database_path)
    if problem:
        return {"status": "blocked", "reason": problem}
    destination = Path(plist_path).expanduser()
    had_plist = destination.exists()
    loaded, detail, inspect_error = _inspect_remove_job(label, user_id, runner)
    if inspect_error:
        return {"status": "blocked", "reason": inspect_error}
    if destination.is_symlink():
        return {"status": "blocked", "reason": "LaunchAgent plist is a symlink; ownership cannot be removed safely"}
    if destination.exists():
        if not _valid_existing_job(destination, label, repo):
            return {"status": "blocked", "reason": "LaunchAgent file ownership cannot be proven for this Consumer"}
        try:
            with destination.open("rb") as stream:
                job = plistlib.load(stream)
            args = job.get("ProgramArguments", [])
            configured_runtime = args[args.index("--runtime-config") + 1]
            if configured_runtime != str(Path(runtime_config_path).expanduser().resolve()):
                return {"status": "blocked", "reason": "LaunchAgent runtime config does not match the requested Consumer config"}
        except (OSError, ValueError, IndexError, TypeError):
            return {"status": "blocked", "reason": "LaunchAgent runtime config ownership cannot be proven"}
    elif loaded:
        return {"status": "blocked", "reason": "LaunchAgent is loaded but its Consumer-owned plist is missing"}
    pid_match = re.search(r"\bpid\s*=\s*(\d+)", detail)
    pid = int(pid_match.group(1)) if pid_match else None
    if loaded:
        if pid:
            process = _run(["ps", "-p", str(pid), "-o", "command="], runner)
            if process.returncode != 0 or "vccp_runtime.runner" not in (process.stdout or ""):
                return {"status": "blocked", "reason": "loaded process identity does not match the VCCP Runner"}
        stopped = _run(["launchctl", "bootout", f"gui/{user_id}/{label}"], runner)
        if stopped.returncode != 0:
            return {"status": "blocked", "reason": "Consumer Runner could not be stopped"}
        still_loaded, _, inspect_error = _inspect_remove_job(label, user_id, runner)
        if inspect_error:
            return {"status": "blocked", "reason": inspect_error}
        if still_loaded:
            return {"status": "blocked", "reason": "Consumer Runner remains loaded after stop"}
        if pid:
            for _ in range(20):
                process = _run(["ps", "-p", str(pid), "-o", "command="], runner)
                if process.returncode != 0:
                    break
                time.sleep(0.1)
            else:
                return {"status": "blocked", "reason": "Consumer Runner process remains after launchd stop"}
        problem = _active_work(repo, database_path)
        if problem:
            return {"status": "blocked", "reason": problem}
    try:
        with RuntimeLock(repo, database_path):
            pass
    except RuntimeAlreadyRunning:
        return {"status": "blocked", "reason": "Consumer RuntimeLock is still held after stop"}
    except OSError as error:
        return {"status": "blocked", "reason": f"RuntimeLock release could not be verified: {error}"}
    if destination.exists():
        destination.unlink()
    return {"status": "removed" if loaded or had_plist else "already_removed",
            "mechanism": "launchd", "label": label, "runner_stopped": True, "runtime_lock_released": True}
