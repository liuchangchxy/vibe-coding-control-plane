"""Native single-user host reconciliation for the production Runner."""
from __future__ import annotations

import os
import hashlib
import json
from contextlib import closing
from pathlib import Path
import plistlib
import re
import subprocess
import sqlite3
import time

from .runner import RuntimeLock, RuntimeAlreadyRunning


def _normalize_path_str(p: str | Path) -> str:
    s = str(p).strip().replace("\\", "/")
    if re.match(r"^[A-Za-z]:/", s):
        return s
    return Path(p).expanduser().resolve().as_posix()


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
                            "SUCCESS_NO_OP",
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


def _valid_existing_sidecar(sidecar_path: Path, repo: str) -> bool:
    try:
        data = json.loads(sidecar_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        return False
    args = data.get("args")
    return (isinstance(args, list)
            and "-m" in args and args[args.index("-m") + 1:args.index("-m") + 2] == ["vccp_runtime.runner"]
            and "--repo" in args and args[args.index("--repo") + 1:args.index("--repo") + 2] == [repo])


def _find_windows_runner_processes(repo: str, runtime_config_path: str | None = None, runner=subprocess.run) -> list[int]:
    """Query running processes matching vccp_runtime.runner, repo, and optionally runtime config."""
    import csv
    import io
    import shlex

    # Query running processes using powershell Get-CimInstance formatted as CSV
    ps_cmd = [
        "powershell", "-NoProfile", "-NonInteractive", "-Command",
        "Get-CimInstance Win32_Process | Select-Object ProcessId, Name, CommandLine | ConvertTo-Csv -NoTypeInformation"
    ]
    ps_res = _run(ps_cmd, runner)
    if ps_res.returncode != 0 or not ps_res.stdout:
        return []

    norm_config = _normalize_path_str(runtime_config_path) if runtime_config_path else None
    matching_pids = []
    try:
        reader = csv.reader(io.StringIO(ps_res.stdout.strip()))
        header = next(reader, None)
        for row in reader:
            if len(row) < 3:
                continue
            pid_str, name, cmd = row[0], row[1], row[2]
            if not ("python" in (name or "").lower() or "py" in (name or "").lower()):
                continue
            if not cmd or "Get-CimInstance" in cmd or "-Command" in cmd:
                continue
            try:
                tokens = shlex.split(cmd, posix=False)
            except ValueError:
                continue
            if "-c" in tokens:
                continue
            if "-m" not in tokens or tokens[tokens.index("-m") + 1:tokens.index("-m") + 2] != ["vccp_runtime.runner"]:
                continue
            if "--repo" not in tokens or tokens[tokens.index("--repo") + 1:tokens.index("--repo") + 2] != [repo]:
                continue
            if norm_config:
                if "--runtime-config" not in tokens:
                    continue
                cfg = tokens[tokens.index("--runtime-config") + 1:tokens.index("--runtime-config") + 2]
                if not cfg or _normalize_path_str(cfg[0].strip("\"'")).casefold() != norm_config.casefold():
                    continue
            try:
                matching_pids.append(int(pid_str))
            except ValueError:
                pass
    except Exception:
        pass
    return matching_pids


def reconcile_windows_sidecar(*, repo: str, runtime_root: str, manifest_path: str,
                              runtime_config_path: str, database_path: str,
                              python_executable: str, sidecar_path: str,
                              readiness, runner=subprocess.run, environ=None) -> dict:
    """Stop, reconcile, qualify, and verify one exact-revision Windows AntiGravity Sidecar."""
    runtime = Path(runtime_root).expanduser().resolve()
    destination = Path(sidecar_path).expanduser()
    if not re.fullmatch(r"[^/]+/[^/]+", repo) or not runtime.is_dir():
        return {"status": "blocked", "reason": "Consumer identity or exact runtime checkout is unavailable"}

    manifest_file = _normalize_path_str(manifest_path)
    config_file = _normalize_path_str(runtime_config_path)
    norm_py = _normalize_path_str(python_executable)
    norm_manifest = manifest_file
    norm_config = config_file
    norm_runtime = _normalize_path_str(runtime)

    expected_args = ["-m", "vccp_runtime.runner", "--manifest", norm_manifest,
                     "--runtime-config", norm_config, "--repo", repo,
                     "--continuous", "--poll-interval", "15"]
    expected_env = {"PYTHONPATH": norm_runtime, "PYTHONUTF8": "1"}

    existing_pids = _find_windows_runner_processes(repo, runtime_config_path, runner)

    if destination.is_file():
        if not _valid_existing_sidecar(destination, repo):
            return {"status": "blocked", "reason": "existing sidecar file belongs to another job"}
        try:
            current = json.loads(destination.read_text(encoding="utf-8"))
            current_cmd = _normalize_path_str(current.get("command", ""))
            current_args = current.get("args")
            current_env = current.get("env") or {}
            if (current_cmd.casefold() == norm_py.casefold()
                    and current_args == expected_args
                    and current_env.get("PYTHONPATH") == norm_runtime
                    and current_env.get("PYTHONUTF8") == "1"
                    and len(existing_pids) == 1):
                return {"status": "running", "no_op": True, "mechanism": "windows_sidecar",
                        "sidecar_path": str(destination), "pid": existing_pids[0],
                        "revision": runtime.name, "runtime_root": str(runtime)}
        except (OSError, json.JSONDecodeError):
            pass

    # If runner processes exist, stop them safely before updating sidecar
    if existing_pids:
        for pid in existing_pids:
            stopped = _run(["taskkill", "/PID", str(pid), "/T", "/F"], runner)
            if stopped.returncode != 0:
                return {"status": "blocked", "reason": f"existing Consumer Runner process {pid} could not be stopped"}
        # Wait up to 5s for lock to be released
        time.sleep(0.5)
        for _ in range(20):
            if not _find_windows_runner_processes(repo, runtime_config_path, runner):
                break
            time.sleep(0.2)
        else:
            return {"status": "blocked", "reason": "existing Consumer Runner processes still active after stop"}

    definition = {
        "display_name": f"{repo} VCCP Generic Runtime",
        "description": f"Authoritative VCCP Generic Runtime for {repo}",
        "command": norm_py,
        "args": expected_args,
        "restart_policy": "always",
        "env": expected_env,
    }

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + f".{os.getpid()}.tmp")
    try:
        temporary.write_text(json.dumps(definition, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(temporary, destination)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass

    qualified = readiness()
    if not isinstance(qualified, dict) or qualified.get("status") != "ready":
        return {"status": "blocked", "reason": "machine readiness did not pass", "readiness": qualified}

    # Verify RuntimeLock is acquirable now that old runner is stopped
    try:
        with RuntimeLock(repo, database_path):
            pass
    except RuntimeAlreadyRunning:
        return {"status": "blocked", "reason": "Consumer RuntimeLock is still held after stopping prior Runner"}

    # Verify that the Runner process is observed running under the reconciled config
    observed_pids = []
    for _ in range(20):
        observed_pids = _find_windows_runner_processes(repo, runtime_config_path, runner)
        if len(observed_pids) == 1:
            break
        time.sleep(0.2)

    if len(observed_pids) != 1:
        return {"status": "blocked", "reason": "reconciled Consumer Runner process is not running"}

    return {"status": "running", "mechanism": "windows_sidecar", "sidecar_path": str(destination),
            "pid": observed_pids[0], "revision": runtime.name, "runtime_root": str(runtime)}


def remove_windows_sidecar(*, repo: str, database_path: str, runtime_config_path: str,
                           sidecar_path: str, runner=subprocess.run) -> dict:
    """Stop one proven Consumer sidecar Runner, verify process/lock released, then remove its sidecar."""
    problem = _active_work(repo, database_path)
    if problem:
        return {"status": "blocked", "reason": problem}

    destination = Path(sidecar_path).expanduser()
    had_sidecar = destination.exists()

    if destination.is_symlink():
        return {"status": "blocked", "reason": "Sidecar config is a symlink; ownership cannot be removed safely"}

    if destination.exists():
        if not _valid_existing_sidecar(destination, repo):
            return {"status": "blocked", "reason": "Sidecar config ownership cannot be proven for this Consumer"}
        try:
            current = json.loads(destination.read_text(encoding="utf-8"))
            args = current.get("args", [])
            configured_runtime = args[args.index("--runtime-config") + 1]
            if _normalize_path_str(configured_runtime).casefold() != _normalize_path_str(runtime_config_path).casefold():
                return {"status": "blocked", "reason": "Sidecar runtime config does not match the requested Consumer config"}
        except (OSError, ValueError, IndexError, TypeError, json.JSONDecodeError):
            return {"status": "blocked", "reason": "Sidecar runtime config ownership cannot be proven"}

    existing_pids = _find_windows_runner_processes(repo, runtime_config_path, runner)
    if existing_pids:
        for pid in existing_pids:
            stopped = _run(["taskkill", "/PID", str(pid), "/T", "/F"], runner)
            if stopped.returncode != 0:
                return {"status": "blocked", "reason": f"Consumer Runner process {pid} could not be stopped"}
        time.sleep(0.5)
        for _ in range(20):
            if not _find_windows_runner_processes(repo, runtime_config_path, runner):
                break
            time.sleep(0.2)
        else:
            return {"status": "blocked", "reason": "Consumer Runner process remains after stop"}

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

    return {"status": "removed" if had_sidecar or existing_pids else "already_removed",
            "mechanism": "windows_sidecar", "sidecar_path": str(destination),
            "runner_stopped": True, "runtime_lock_released": True}
