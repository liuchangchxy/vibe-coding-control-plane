"""Native single-user host reconciliation for the production Runner."""
from __future__ import annotations

import os
import hashlib
import json
import html
import getpass
from contextlib import closing
from pathlib import Path
import plistlib
import re
import subprocess
import sqlite3
import time
import xml.etree.ElementTree as ET

from .runner import RuntimeLock, RuntimeAlreadyRunning
from .phases import is_terminal_attempt_phase


def _windows_task_name(repo: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        raise ValueError("invalid Consumer repository identity")
    suffix = hashlib.sha256(repo.casefold().encode()).hexdigest()[:16]
    return f"VCCP-Consumer-{suffix}"


def _runtime_config_digest(path: str | Path) -> str:
    config = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    if isinstance(config, dict):
        config.pop("upgrade_target", None)
    canonical = json.dumps(config, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _windows_query(task_name, runner):
    result = _run(["schtasks.exe", "/Query", "/TN", task_name, "/XML"], runner)
    if result.returncode:
        detail = f"{result.stdout or ''}\n{result.stderr or ''}".casefold()
        if not detail.strip():
            check = _run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                          f"try {{ $tasks = Get-ScheduledTask -ErrorAction Stop; "
                          f"if (@($tasks | Where-Object TaskName -eq '{task_name}').Count -eq 0) "
                          "{ exit 3 } else { exit 0 } } catch { exit 4 }"], runner)
            if check.returncode == 3:
                return None
            raise ValueError(f"Windows Task Scheduler state could not be inspected: {detail.strip()}")
        if any(phrase in detail for phrase in ("cannot find the file", "does not exist", "cannot find the task",
                                               "cannot find the path specified")):
            return None
        raise ValueError(f"Windows Task Scheduler state could not be inspected: {detail.strip()}")
    return ET.fromstring(result.stdout.lstrip("\ufeff"))


def _windows_processes(runner):
    command = ("Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like "
               "'*vccp_runtime.runner*' } | Select-Object ProcessId,ExecutablePath,CommandLine | ConvertTo-Json -Compress")
    result = _run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command], runner)
    if result.returncode:
        raise ValueError("Windows Runner process state could not be inspected")
    output = (result.stdout or "").strip()
    if not output:
        return []
    values = json.loads(output)
    return values if isinstance(values, list) else [values]


def _consumer_windows_processes(repo, runner):
    identity = re.compile(r"(?:^|\s)--repo(?:\s+|=)[\"']?" + re.escape(repo) + r"[\"']?(?=\s|$)", re.I)
    return [item for item in _windows_processes(runner)
            if identity.search(str(item.get("CommandLine", "")))]


def _wait_windows_stopped(repo, runner):
    for _ in range(20):
        if not _consumer_windows_processes(repo, runner):
            return True
        time.sleep(0.25)
    return False


def _restore_windows_task(task_name, previous, runner):
    if previous is None:
        return False
    temporary = Path(os.environ.get("TEMP", str(Path.home()))) / f"vccp-restore-{os.getpid()}.xml"
    try:
        previous_xml = ET.tostring(previous, encoding="unicode")
        temporary.write_text(f'<?xml version="1.0" encoding="UTF-16"?>{previous_xml}', encoding="utf-16")
        created = _run(["schtasks.exe", "/Create", "/TN", task_name, "/XML", str(temporary), "/F"], runner)
        if created.returncode or _run(["schtasks.exe", "/Run", "/TN", task_name], runner).returncode:
            return False
        restored = ET.fromstring(temporary.read_text(encoding="utf-16").lstrip("\ufeff"))
        action = restored.find(".//{*}Actions/{*}Exec")
        arguments = action.findtext("{*}Arguments", "") if action is not None else ""
        repo = re.search(r"--repo\s+([^\s\"]+)", arguments)
        if action is None or not repo:
            return False
        expected_command = str(Path(action.findtext("{*}Command", "")).resolve()).casefold()
        expected_arguments = re.sub(r"\s+", " ", arguments).strip().casefold()
        try:
            for _ in range(20):
                processes = _consumer_windows_processes(repo.group(1), runner)
                if any(re.sub(r"\s+", " ", str(item.get("CommandLine", "")).strip()).casefold().endswith(expected_arguments)
                       and str(Path(item.get("ExecutablePath", "")).resolve()).casefold() == expected_command
                       for item in processes):
                    return True
                time.sleep(0.25)
            return False
        except (OSError, ValueError, json.JSONDecodeError):
            return False
    finally:
        temporary.unlink(missing_ok=True)


def inspect_windows_task(*, repo: str, manifest_path: str, runtime_config_path: str,
                         runner=subprocess.run) -> dict:
    task_name = _windows_task_name(repo)
    try:
        task = _windows_query(task_name, runner)
        if task is None:
            return {"mechanism": "windows_task_scheduler", "status": "stopped",
                    "running_revision": None, "running_checkout_clean": False, "config_matches": False}
        description = task.findtext(".//{*}RegistrationInfo/{*}Description", "")
        expected_owner = f"VCCP Consumer {repo.casefold()}"
        if not description.startswith(expected_owner):
            return {"mechanism": "windows_task_scheduler", "status": "blocked",
                    "reason": "scheduled task ownership does not match this Consumer"}
        action = task.find(".//{*}Actions/{*}Exec")
        if action is None:
            return {"mechanism": "windows_task_scheduler", "status": "drift"}
        working_directory = action.findtext("{*}WorkingDirectory")
        revision_match = re.search(r"runtime_revision=([0-9a-f]{40})", description, re.I)
        manifest_hash = hashlib.sha256(Path(manifest_path).expanduser().read_bytes()).hexdigest()
        config_hash = _runtime_config_digest(runtime_config_path)
        digest_match = re.search(r"manifest_sha256=([0-9a-f]{64}) config_sha256=([0-9a-f]{64})", description, re.I)
        config_matches = bool(digest_match and digest_match.group(1).lower() == manifest_hash
                              and digest_match.group(2).lower() == config_hash)
        consumer_processes = _consumer_windows_processes(repo, runner)
        process = next((item for item in consumer_processes
                        if f"--repo {repo}".casefold() in str(item.get("CommandLine", "")).casefold()
                        and str(Path(action.findtext("{*}Command", "")).resolve()).casefold()
                        == str(Path(item.get("ExecutablePath", "")).resolve()).casefold()
                        and str(Path(manifest_path).expanduser().resolve()).casefold()
                        in str(item.get("CommandLine", "")).casefold()
                        and str(Path(runtime_config_path).expanduser().resolve()).casefold()
                        in str(item.get("CommandLine", "")).casefold()), None)
        if consumer_processes and process is None:
            return {"mechanism": "windows_task_scheduler", "status": "blocked",
                    "reason": "running Consumer process does not match its scheduled task definition"}
        revision = revision_match.group(1).lower() if revision_match else None
        clean = False
        if working_directory and revision:
            runtime = Path(working_directory)
            git = subprocess.run(["git", "-C", str(runtime), "status", "--porcelain=v1", "--untracked-files=all"],
                                 text=True, capture_output=True, check=False)
            head = subprocess.run(["git", "-C", str(runtime), "rev-parse", "--verify", "HEAD"],
                                  text=True, capture_output=True, check=False)
            clean = git.returncode == 0 and not git.stdout.strip() and head.returncode == 0 \
                and head.stdout.strip().lower() == revision
        return {"mechanism": "windows_task_scheduler", "status": "running" if process else "stopped",
                "task_name": task_name, "pid": int(process["ProcessId"]) if process else None,
                "running_revision": revision, "running_checkout": working_directory,
                "running_checkout_clean": clean, "config_matches": config_matches}
    except (OSError, ValueError, ET.ParseError, json.JSONDecodeError) as error:
        return {"mechanism": "windows_task_scheduler", "status": "blocked", "reason": str(error)}


def reconcile_windows_task(*, repo: str, runtime_root: str, manifest_path: str,
                            runtime_config_path: str, python_executable: str,
                            readiness, runner=subprocess.run) -> dict:
    runtime = Path(runtime_root).expanduser().resolve()
    manifest = Path(manifest_path).expanduser().resolve()
    config = Path(runtime_config_path).expanduser().resolve()
    if not runtime.is_dir() or not re.fullmatch(r"[0-9a-f]{40}", runtime.name, re.I):
        return {"status": "blocked", "reason": "exact pinned runtime checkout is unavailable"}
    task_name = _windows_task_name(repo)
    manifest_hash = hashlib.sha256(manifest.read_bytes()).hexdigest()
    config_hash = _runtime_config_digest(config)
    description = (f"VCCP Consumer {repo.casefold()} runtime_revision={runtime.name.lower()} "
                   f"manifest_sha256={manifest_hash} config_sha256={config_hash}")
    existing = _windows_query(task_name, runner)
    if existing is not None:
        info = existing.findtext(".//{*}RegistrationInfo/{*}Description", "")
        if not info.startswith(f"VCCP Consumer {repo.casefold()}"):
            return {"status": "blocked", "reason": "scheduled task name is owned by another task"}
        observed = inspect_windows_task(repo=repo, manifest_path=str(manifest),
                                        runtime_config_path=str(config), runner=runner)
        if (observed.get("status") == "running" and observed.get("running_revision") == runtime.name.lower()
                and observed.get("running_checkout_clean") and observed.get("config_matches")):
            return {"status": "running", "no_op": True, **observed}
        stopped = _run(["schtasks.exe", "/End", "/TN", task_name], runner)
        try:
            remaining = _consumer_windows_processes(repo, runner)
        except (OSError, ValueError, json.JSONDecodeError):
            restored = _restore_windows_task(task_name, existing, runner)
            return {"status": "blocked", "reason": "Windows Runner process state could not be inspected",
                    "previous_runner_restored": restored}
        if stopped.returncode and remaining:
            return {"status": "blocked", "reason": "existing Consumer Runner could not be stopped"}
        try:
            stopped = _wait_windows_stopped(repo, runner)
        except (OSError, ValueError, json.JSONDecodeError):
            restored = _restore_windows_task(task_name, existing, runner)
            return {"status": "blocked", "reason": "Windows Runner process state could not be inspected",
                    "previous_runner_restored": restored}
        if not stopped:
            return {"status": "blocked", "reason": "Consumer Runner process remains after stop"}
    qualified = readiness(runtime)
    if not isinstance(qualified, dict) or qualified.get("status") != "ready":
        restored = _restore_windows_task(task_name, existing, runner)
        return {"status": "blocked", "reason": "machine readiness did not pass",
                "readiness": qualified, "previous_runner_restored": restored if existing is not None else None}
    escaped_description = html.escape(description)
    username = os.environ.get("USERNAME") or getpass.getuser()
    domain = os.environ.get("USERDOMAIN")
    user_id = f"{domain}\\{username}" if domain else username
    escaped_user_id = html.escape(user_id)
    escaped_command = html.escape(str(Path(python_executable).resolve()))
    escaped_args = html.escape(f'-u -m vccp_runtime.runner --manifest "{manifest}" --runtime-config "{config}" --repo {repo} --continuous --poll-interval 30')
    escaped_cwd = html.escape(str(runtime))
    xml = (f'<?xml version="1.0" encoding="UTF-16"?>'
           f'<Task version="1.3" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">'
           f'<RegistrationInfo><Description>{escaped_description}</Description></RegistrationInfo>'
           f'<Triggers><LogonTrigger><Enabled>true</Enabled><UserId>{escaped_user_id}</UserId></LogonTrigger></Triggers>'
           f'<Principals><Principal id="Author"><UserId>{escaped_user_id}</UserId><LogonType>InteractiveToken</LogonType>'
           f'<RunLevel>LeastPrivilege</RunLevel></Principal></Principals>'
           f'<Settings><MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy><StartWhenAvailable>true</StartWhenAvailable>'
           f'<RestartOnFailure><Interval>PT1M</Interval><Count>255</Count></RestartOnFailure>'
           f'<ExecutionTimeLimit>PT0S</ExecutionTimeLimit><Enabled>true</Enabled><Hidden>true</Hidden></Settings>'
           f'<Actions Context="Author"><Exec><Command>{escaped_command}</Command><Arguments>{escaped_args}</Arguments>'
           f'<WorkingDirectory>{escaped_cwd}</WorkingDirectory></Exec></Actions></Task>')
    temporary = Path(os.environ.get("TEMP", str(Path.home()))) / f"vccp-{hashlib.sha256(repo.casefold().encode()).hexdigest()[:16]}.xml"
    temporary.write_text(xml, encoding="utf-16")
    try:
        created = _run(["schtasks.exe", "/Create", "/TN", task_name, "/XML", str(temporary), "/F"], runner)
        if created.returncode:
            restored = _restore_windows_task(task_name, existing, runner)
            return {"status": "blocked", "reason": "Consumer scheduled task could not be registered",
                    "previous_runner_restored": restored if existing is not None else None}
        started = _run(["schtasks.exe", "/Run", "/TN", task_name], runner)
        if started.returncode:
            restored = _restore_windows_task(task_name, existing, runner)
            return {"status": "blocked", "reason": "Consumer Runner task could not be started",
                    "previous_runner_restored": restored if existing is not None else None}
        observed = None
        for _ in range(20):
            observed = inspect_windows_task(repo=repo, manifest_path=str(manifest),
                                            runtime_config_path=str(config), runner=runner)
            if (observed.get("status") == "running" and observed.get("running_revision") == runtime.name.lower()
                    and observed.get("running_checkout_clean") and observed.get("config_matches")):
                return {"status": "running", **observed}
            time.sleep(0.25)
        restored = _restore_windows_task(task_name, existing, runner)
        return {"status": "blocked", "reason": "scheduled task did not produce the pinned running Consumer Runner",
                "host": observed, "previous_runner_restored": restored if existing is not None else None}
    finally:
        temporary.unlink(missing_ok=True)


def remove_windows_task(*, repo: str, database_path: str, runtime_config_path: str,
                        runner=subprocess.run) -> dict:
    problem = _active_work(repo, database_path)
    if problem:
        return {"status": "blocked", "reason": problem}
    task_name = _windows_task_name(repo)
    task = _windows_query(task_name, runner)
    if task is None:
        return {"status": "already_removed", "mechanism": "windows_task_scheduler"}
    description = task.findtext(".//{*}RegistrationInfo/{*}Description", "")
    if not description.startswith(f"VCCP Consumer {repo.casefold()}"):
        return {"status": "blocked", "reason": "scheduled task ownership cannot be proven for this Consumer"}
    action = task.find(".//{*}Actions/{*}Exec")
    arguments = action.findtext("{*}Arguments", "") if action is not None else ""
    if ("vccp_runtime.runner" not in arguments
            or f"--repo {repo}".casefold() not in arguments.casefold()
            or str(Path(runtime_config_path).expanduser().resolve()).casefold() not in arguments.casefold()):
        return {"status": "blocked", "reason": "scheduled task runtime config does not match this Consumer"}
    ended = _run(["schtasks.exe", "/End", "/TN", task_name], runner)
    # An already idle task can report failure for /End; process observation is authoritative.
    try:
        remaining = _consumer_windows_processes(repo, runner)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        return {"status": "blocked", "reason": f"Windows Runner process state could not be inspected: {error}"}
    if remaining and not _wait_windows_stopped(repo, runner):
        return {"status": "blocked", "reason": "Consumer Runner process remains after stop"}
    deleted = _run(["schtasks.exe", "/Delete", "/TN", task_name, "/F"], runner)
    if deleted.returncode:
        return {"status": "blocked", "reason": "Consumer scheduled task could not be removed"}
    try:
        with RuntimeLock(repo, database_path):
            pass
    except RuntimeAlreadyRunning:
        return {"status": "blocked", "reason": "Consumer RuntimeLock is still held after stop"}
    except OSError as error:
        return {"status": "blocked", "reason": f"RuntimeLock release could not be verified: {error}"}
    return {"status": "removed", "mechanism": "windows_task_scheduler", "runner_stopped": True,
            "runtime_lock_released": True}


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


def _restore_macos_job(destination, old, was_loaded, label, user_id, runner):
    active, _ = _loaded(label, user_id, runner)
    if active and _run(["launchctl", "bootout", f"gui/{user_id}/{label}"], runner).returncode != 0:
        return False
    if old is None:
        destination.unlink(missing_ok=True)
        return not was_loaded
    temporary = destination.with_name(destination.name + f".{os.getpid()}.restore.tmp")
    try:
        with temporary.open("wb") as stream:
            plistlib.dump(old, stream, sort_keys=True)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    if not was_loaded:
        return True
    if _run(["launchctl", "bootstrap", f"gui/{user_id}", str(destination)], runner).returncode != 0:
        return False
    if _run(["launchctl", "kickstart", "-k", f"gui/{user_id}/{label}"], runner).returncode != 0:
        return False
    active, detail = _loaded(label, user_id, runner)
    match = re.search(r"\bpid\s*=\s*(\d+)", detail)
    if not active or not match:
        return False
    process = _run(["ps", "-p", match.group(1), "-o", "command="], runner)
    return process.returncode == 0 and "vccp_runtime.runner" in (process.stdout or "")


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
    was_loaded = loaded
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
        config_digest = _runtime_config_digest(runtime_config_path)
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
        "VCCPRuntimeConfigSHA256": _runtime_config_digest(runtime_config_path),
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
        restored = _restore_macos_job(destination, old or None, was_loaded, label, user_id, runner)
        return {"status": "blocked", "reason": "machine readiness did not pass", "readiness": qualified,
                "previous_runner_restored": restored if was_loaded else None}
    bootstrap = _run(["launchctl", "bootstrap", f"gui/{user_id}", str(destination)], runner)
    if bootstrap.returncode != 0:
        restored = _restore_macos_job(destination, old or None, was_loaded, label, user_id, runner)
        return {"status": "blocked", "reason": "LaunchAgent could not be installed",
                "previous_runner_restored": restored if was_loaded else None}
    started = _run(["launchctl", "kickstart", "-k", f"gui/{user_id}/{label}"], runner)
    if started.returncode != 0:
        restored = _restore_macos_job(destination, old or None, was_loaded, label, user_id, runner)
        return {"status": "blocked", "reason": "Consumer Runner could not be started",
                "previous_runner_restored": restored if was_loaded else None}
    active, detail = _loaded(label, user_id, runner)
    match = re.search(r"\bpid\s*=\s*(\d+)", detail)
    pid = int(match.group(1)) if match else None
    if not active or not pid:
        restored = _restore_macos_job(destination, old or None, was_loaded, label, user_id, runner)
        return {"status": "blocked", "reason": "LaunchAgent did not produce one running process",
                "previous_runner_restored": restored if was_loaded else None}
    process = _run(["ps", "-p", str(pid), "-o", "command="], runner)
    if process.returncode != 0 or "vccp_runtime.runner" not in (process.stdout or ""):
        restored = _restore_macos_job(destination, old or None, was_loaded, label, user_id, runner)
        return {"status": "blocked", "reason": "LaunchAgent process does not match the VCCP Runner",
                "previous_runner_restored": restored if was_loaded else None}
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
                active = [row[0] for row in rows if not is_terminal_attempt_phase(row[0], bool(row[1]))]
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
