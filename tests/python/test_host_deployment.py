import plistlib
import hashlib
import json
from contextlib import closing
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path
from types import SimpleNamespace
import sys
import xml.etree.ElementTree as ET

from vccp_runtime.host_deployment import (inspect_windows_task, reconcile_macos_launch_agent,
    reconcile_windows_task, remove_macos_launch_agent, remove_windows_task, _consumer_windows_processes,
    _runtime_config_digest)


class HostDeploymentTests(unittest.TestCase):
    def test_windows_empty_failed_query_confirms_missing_task_then_reconcile_creates(self):
        import subprocess
        import vccp_runtime.host_deployment as deployment
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            runtime = root / "runtime"
            runtime.mkdir()
            subprocess.run(["git", "init", "-q", str(runtime)], check=True)
            subprocess.run(["git", "-C", str(runtime), "config", "user.email", "test@example.com"], check=True)
            subprocess.run(["git", "-C", str(runtime), "config", "user.name", "Test"], check=True)
            (runtime / "runtime.py").write_text("pinned\n")
            subprocess.run(["git", "-C", str(runtime), "add", "."], check=True)
            subprocess.run(["git", "-C", str(runtime), "commit", "-qm", "pinned"], check=True)
            revision = subprocess.check_output(["git", "-C", str(runtime), "rev-parse", "HEAD"], text=True).strip()
            pinned = root / revision
            runtime.rename(pinned)
            manifest, config = root / "manifest.yml", root / "runtime.json"
            manifest.write_text("manifest\n")
            config.write_text("{}\n")
            calls = []
            class TaskCreated(Exception):
                pass
            def runner(command, **_kwargs):
                calls.append(command)
                if command[:2] == ["schtasks.exe", "/Query"]:
                    return SimpleNamespace(returncode=1, stdout="", stderr="")
                if command[0] == "powershell.exe":
                    return SimpleNamespace(returncode=3, stdout="", stderr="")
                if command[:2] == ["schtasks.exe", "/Create"]:
                    raise TaskCreated
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            with self.assertRaises(TaskCreated):
                reconcile_windows_task(repo="owner/consumer", runtime_root=str(pinned),
                    manifest_path=str(manifest), runtime_config_path=str(config),
                    python_executable=sys.executable, readiness=lambda _runtime: {"status": "ready"},
                    runner=runner)
            self.assertTrue(any(c[:2] == ["schtasks.exe", "/Create"] for c in calls))
            self.assertTrue(any(c[0] == "powershell.exe" and "Schedule.Service" in c[-1] for c in calls))

    def test_windows_empty_query_and_task_scheduler_check_failure_blocks(self):
        import vccp_runtime.host_deployment as deployment
        def runner(command, **_kwargs):
            if command[:2] == ["schtasks.exe", "/Query"]:
                return SimpleNamespace(returncode=1, stdout="", stderr="")
            return SimpleNamespace(returncode=4, stdout="", stderr="Access denied")
        with self.assertRaisesRegex(ValueError, "state could not be inspected"):
            deployment._windows_query("VCCP-Consumer-example", runner)

    def test_windows_process_repo_match_uses_exact_argument_value(self):
        import vccp_runtime.host_deployment as deployment
        processes = [
            {"ProcessId": 1, "CommandLine": "python -m vccp_runtime.runner --repo owner/app --continuous"},
            {"ProcessId": 2, "CommandLine": "python -m vccp_runtime.runner --repo owner/app2 --continuous"},
        ]
        with patch.object(deployment, "_windows_processes", return_value=processes):
            self.assertEqual([row["ProcessId"] for row in _consumer_windows_processes("owner/app", None)], [1])

    def test_windows_status_verifies_task_process_pinned_checkout_and_config_digests(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            runtime = root / "runtime"
            runtime.mkdir()
            import subprocess
            subprocess.run(["git", "init", "-q", str(runtime)], check=True)
            subprocess.run(["git", "-C", str(runtime), "config", "user.email", "test@example.com"], check=True)
            subprocess.run(["git", "-C", str(runtime), "config", "user.name", "Test"], check=True)
            (runtime / "runtime.py").write_text("pinned\n")
            subprocess.run(["git", "-C", str(runtime), "add", "."], check=True)
            subprocess.run(["git", "-C", str(runtime), "commit", "-qm", "pinned"], check=True)
            revision = subprocess.check_output(["git", "-C", str(runtime), "rev-parse", "HEAD"], text=True).strip()
            runtime.rename(root / revision)
            runtime = root / revision
            manifest, config = root / "manifest.yml", root / "runtime.json"
            manifest.write_text("manifest\n")
            config.write_text("{}\n")
            repo = "owner/consumer"
            suffix = hashlib.sha256(repo.casefold().encode()).hexdigest()[:16]
            task_name = f"VCCP-Consumer-{suffix}"
            description = (f"VCCP Consumer {repo} runtime_revision={revision} "
                f"manifest_sha256={hashlib.sha256(manifest.read_bytes()).hexdigest()} "
                f"config_sha256={_runtime_config_digest(config)}")
            args = (f'-u -m vccp_runtime.runner --manifest "{manifest.resolve()}" '
                    f'--runtime-config "{config.resolve()}" --repo {repo} --continuous --poll-interval 30')
            xml = (f'<Task xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task"><RegistrationInfo>'
                   f'<Description>{description}</Description></RegistrationInfo><Actions><Exec>'
                   f'<Command>{Path(sys.executable).resolve()}</Command><Arguments>{args}</Arguments>'
                   f'<WorkingDirectory>{runtime.resolve()}</WorkingDirectory></Exec></Actions></Task>')
            def runner(command, **_kwargs):
                if command[0] == "schtasks.exe":
                    return SimpleNamespace(returncode=0, stdout=xml, stderr="")
                if command[0] == "powershell.exe":
                    process = {"ProcessId": 41, "ExecutablePath": str(Path(sys.executable).resolve()),
                               "CommandLine": f'{sys.executable} {args}'}
                    return SimpleNamespace(returncode=0, stdout=json.dumps(process), stderr="")
                self.fail(command)
            result = inspect_windows_task(repo=repo, manifest_path=str(manifest),
                runtime_config_path=str(config), runner=runner)
            self.assertEqual(result["status"], "running", result)
            self.assertEqual(result["pid"], 41)
            self.assertEqual(result["running_revision"], revision)
            self.assertTrue(result["running_checkout_clean"])
            self.assertTrue(result["config_matches"])

    def test_windows_readiness_failure_restores_the_previous_runner(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            runtime = root / ("b" * 40)
            runtime.mkdir()
            manifest, config = root / "manifest.yml", root / "runtime.json"
            manifest.write_text("manifest\n")
            config.write_text("{}\n")
            repo = "owner/consumer"
            description = f"VCCP Consumer {repo} runtime_revision={'a' * 40} manifest_sha256={'0' * 64} config_sha256={'0' * 64}"
            executable = str(Path(sys.executable).resolve())
            arguments = f'-m vccp_runtime.runner --runtime-config "{config.resolve()}" --repo {repo} --manifest "{manifest.resolve()}"'
            old_xml = (f'<Task xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task"><RegistrationInfo>'
                       f'<Description>{description}</Description></RegistrationInfo><Actions><Exec>'
                       f'<Command>{executable}</Command><Arguments>{arguments}</Arguments>'
                       f'</Exec></Actions></Task>')
            calls = []
            ended = False
            run_count = 0
            def runner(command, **_kwargs):
                nonlocal ended, run_count
                calls.append(command)
                if command[0] == "schtasks.exe" and command[1] == "/Query":
                    return SimpleNamespace(returncode=0, stdout=old_xml, stderr="")
                if command[0] == "powershell.exe":
                    if ended and run_count == 0:
                        return SimpleNamespace(returncode=0, stdout="", stderr="")
                    return SimpleNamespace(returncode=0, stdout=json.dumps({
                        "ProcessId": 42, "ExecutablePath": executable,
                        "CommandLine": f'{executable} {arguments}'}), stderr="")
                if command[:2] == ["schtasks.exe", "/End"]:
                    ended = True
                    return SimpleNamespace(returncode=0, stdout="", stderr="")
                if command[:2] == ["schtasks.exe", "/Run"]:
                    run_count += 1
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            result = reconcile_windows_task(repo=repo, runtime_root=str(runtime),
                manifest_path=str(manifest), runtime_config_path=str(config),
                python_executable=sys.executable, readiness=lambda _runtime: {"status": "blocked"},
                runner=runner)
            self.assertEqual(result["status"], "blocked")
            self.assertTrue(result["previous_runner_restored"])
            self.assertTrue(any(call[:2] == ["schtasks.exe", "/Run"] for call in calls))

    def test_windows_remove_stops_only_the_owned_task_and_verifies_runtime_lock(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            repo = "owner/consumer"
            config = root / "runtime.json"
            config.write_text("{}\n")
            runtime_config = str(config.resolve())
            description = f"VCCP Consumer {repo} runtime_revision={'a' * 40} manifest_sha256={'0' * 64} config_sha256={'0' * 64}"
            xml = (f'<Task xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task"><RegistrationInfo>'
                   f'<Description>{description}</Description></RegistrationInfo><Actions><Exec>'
                   f'<Command>{Path(sys.executable).resolve()}</Command><Arguments>-m vccp_runtime.runner '
                   f'--runtime-config "{runtime_config}" --repo {repo}</Arguments></Exec></Actions></Task>')
            calls = []
            def runner(command, **_kwargs):
                calls.append(command)
                if command[0] == "schtasks.exe" and command[1] == "/Query":
                    return SimpleNamespace(returncode=0, stdout=xml, stderr="")
                if command[0] == "powershell.exe":
                    return SimpleNamespace(returncode=0, stdout="", stderr="")
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            result = remove_windows_task(repo=repo, database_path=str(root / "state.sqlite"),
                runtime_config_path=runtime_config, runner=runner)
            self.assertEqual(result["status"], "removed", result)
            self.assertTrue(result["runtime_lock_released"])
            self.assertTrue(any(call[:2] == ["schtasks.exe", "/End"] for call in calls))
            self.assertTrue(any(call[:2] == ["schtasks.exe", "/Delete"] for call in calls))

    def test_windows_reconcile_restarts_an_idle_registered_task_when_end_fails(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            runtime = root / ("b" * 40)
            runtime.mkdir()
            manifest, config = root / "manifest.yml", root / "runtime.json"
            manifest.write_text("manifest\n")
            config.write_text("{}\n")
            repo = "owner/consumer"
            old_xml = (f'<Task xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task"><RegistrationInfo>'
                       f'<Description>VCCP Consumer {repo} runtime_revision={"a" * 40}</Description></RegistrationInfo>'
                       f'<Actions><Exec><Command>{sys.executable}</Command><Arguments>-m vccp_runtime.runner '
                       f'--repo {repo}</Arguments></Exec></Actions></Task>')
            running = False
            calls = []
            def runner(command, **_kwargs):
                nonlocal running
                calls.append(command)
                if command[:2] == ["schtasks.exe", "/Query"]:
                    return SimpleNamespace(returncode=0, stdout=old_xml, stderr="")
                if command[:2] == ["schtasks.exe", "/End"]:
                    return SimpleNamespace(returncode=1, stdout="", stderr="no running instance")
                if command[:2] == ["schtasks.exe", "/Create"]:
                    return SimpleNamespace(returncode=0, stdout="", stderr="")
                if command[:2] == ["schtasks.exe", "/Run"]:
                    running = True
                    return SimpleNamespace(returncode=0, stdout="", stderr="")
                if command[0] == "powershell.exe":
                    return SimpleNamespace(returncode=0, stdout="", stderr="")
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            result = reconcile_windows_task(repo=repo, runtime_root=str(runtime), manifest_path=str(manifest),
                runtime_config_path=str(config), python_executable=sys.executable,
                readiness=lambda _runtime: {"status": "ready"}, runner=runner)
            self.assertEqual(result["status"], "blocked", result)  # no exact pinned process was launched
            self.assertTrue(running)
            self.assertTrue(any(call[:2] == ["schtasks.exe", "/Create"] for call in calls))
            self.assertTrue(any(call[:2] == ["schtasks.exe", "/Run"] for call in calls))

    def test_windows_deploy_status_remove_converges_on_one_pinned_runner(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            runtime = root / "runtime"
            runtime.mkdir()
            import subprocess
            subprocess.run(["git", "init", "-q", str(runtime)], check=True)
            subprocess.run(["git", "-C", str(runtime), "config", "user.email", "test@example.com"], check=True)
            subprocess.run(["git", "-C", str(runtime), "config", "user.name", "Test"], check=True)
            (runtime / "runtime.py").write_text("pinned\n")
            subprocess.run(["git", "-C", str(runtime), "add", "."], check=True)
            subprocess.run(["git", "-C", str(runtime), "commit", "-qm", "pinned"], check=True)
            revision = subprocess.check_output(["git", "-C", str(runtime), "rev-parse", "HEAD"], text=True).strip()
            runtime.rename(root / revision)
            runtime = root / revision
            manifest, config = root / "manifest.yml", root / "runtime.json"
            manifest.write_text("manifest\n")
            config.write_text("{}\n")
            repo = "owner/consumer"
            current_task = None
            running = False
            end_idle = True
            old_description = f"VCCP Consumer {repo} runtime_revision={'a' * 40} manifest_sha256={'0' * 64} config_sha256={'0' * 64}"
            old_task = (f'<Task xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task"><RegistrationInfo>'
                        f'<Description>{old_description}</Description></RegistrationInfo><Actions><Exec>'
                        f'<Command>{sys.executable}</Command><Arguments>-m vccp_runtime.runner '
                        f'--runtime-config "{config}" --repo {repo}</Arguments></Exec></Actions></Task>')

            def runner(command, **_kwargs):
                nonlocal current_task, running, end_idle
                if command[0] == "schtasks.exe":
                    if command[1] == "/Query":
                        if current_task is None:
                            return SimpleNamespace(returncode=1, stdout="", stderr="ERROR: The system cannot find the file specified.")
                        return SimpleNamespace(returncode=0, stdout=current_task, stderr="")
                    if command[1] == "/Create":
                        xml_path = Path(command[command.index("/XML") + 1])
                        current_task = xml_path.read_text(encoding="utf-16")
                    elif command[1] == "/Run":
                        running = True
                    elif command[1] == "/End":
                        running = False
                        if end_idle:
                            return SimpleNamespace(returncode=1, stdout="", stderr="ERROR: no running instance")
                    elif command[1] == "/Delete":
                        current_task = None
                    return SimpleNamespace(returncode=0, stdout="", stderr="")
                if command[0] == "powershell.exe":
                    if not running or current_task is None:
                        return SimpleNamespace(returncode=0, stdout="", stderr="")
                    task = ET.fromstring(current_task)
                    action = task.find(".//{*}Actions/{*}Exec")
                    process = {"ProcessId": 47, "ExecutablePath": action.findtext("{*}Command"),
                        "CommandLine": action.findtext("{*}Command") + " " + action.findtext("{*}Arguments")}
                    return SimpleNamespace(returncode=0, stdout=json.dumps(process), stderr="")
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            current_task = old_task
            deployed = reconcile_windows_task(repo=repo, runtime_root=str(runtime),
                manifest_path=str(manifest), runtime_config_path=str(config),
                python_executable=sys.executable, readiness=lambda _path: {"status": "ready"}, runner=runner)
            status = inspect_windows_task(repo=repo, manifest_path=str(manifest),
                runtime_config_path=str(config), runner=runner)
            removed = remove_windows_task(repo=repo, database_path=str(root / "state.sqlite"),
                runtime_config_path=str(config), runner=runner)
            after_remove = inspect_windows_task(repo=repo, manifest_path=str(manifest),
                runtime_config_path=str(config), runner=runner)
            self.assertEqual(deployed["status"], "running", deployed)
            self.assertEqual(status["status"], "running", status)
            self.assertEqual(status["running_revision"], revision)
            self.assertTrue(status["running_checkout_clean"])
            self.assertTrue(status["config_matches"])
            self.assertEqual(removed["status"], "removed", removed)
            self.assertEqual(after_remove["status"], "stopped")

    def test_remove_stops_verified_consumer_then_removes_its_launcher(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            database = root / "state.sqlite"
            plist = root / "runner.plist"
            plist.write_bytes(plistlib.dumps({"Label": "com.vccp.consumer-dispatcher",
                "ProgramArguments": ["python", "-m", "vccp_runtime.runner", "--runtime-config",
                    str((root / "runtime.json").resolve()), "--repo", "owner/consumer"]}))
            calls = []
            printed = iter(["state = running\npid = 41", ""])
            ps_checks = iter([SimpleNamespace(returncode=0, stdout="python -m vccp_runtime.runner"),
                              SimpleNamespace(returncode=1, stdout="")])
            def command(args, **kwargs):
                calls.append(args)
                if args[:2] == ["launchctl", "print"]:
                    value = next(printed)
                    return SimpleNamespace(returncode=0 if "pid =" in value else 1, stdout=value,
                        stderr="" if "pid =" in value else "Could not find service")
                if args[:2] == ["ps", "-p"]:
                    return next(ps_checks)
                return SimpleNamespace(returncode=0, stdout="")
            result = remove_macos_launch_agent(repo="owner/consumer", database_path=str(database),
                runtime_config_path=str(root / "runtime.json"),
                label="com.vccp.consumer-dispatcher", plist_path=str(plist), user_id="501", runner=command)
            self.assertEqual(result["status"], "removed", result)
            self.assertTrue(result["runtime_lock_released"])
            self.assertFalse(plist.exists())
            self.assertIn(["launchctl", "bootout", "gui/501/com.vccp.consumer-dispatcher"], calls)

    def test_remove_blocks_on_active_implementer_or_reviewer_execution(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            database = root / "state.sqlite"
            import sqlite3
            with closing(sqlite3.connect(database)) as connection:
                connection.execute("CREATE TABLE attempts(repo TEXT, phase TEXT, closeout_pending INTEGER)")
                connection.execute("INSERT INTO attempts VALUES('owner/consumer', 'PR_BOUND', 0)")
                connection.commit()
            result = remove_macos_launch_agent(repo="owner/consumer", database_path=str(database),
                runtime_config_path=str(Path(root) / "runtime.json"),
                label="com.vccp.consumer-dispatcher", plist_path=str(root / "missing.plist"),
                user_id="501", runner=lambda *_args, **_kwargs: self.fail("must block before host mutation"))
            self.assertEqual(result["status"], "blocked")
            self.assertIn("active Consumer execution", result["reason"])

    def test_remove_allows_completed_success_no_op_attempt(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            database = root / "state.sqlite"
            import sqlite3
            with closing(sqlite3.connect(database)) as connection:
                connection.execute("CREATE TABLE attempts(repo TEXT, phase TEXT, closeout_pending INTEGER)")
                connection.execute("INSERT INTO attempts VALUES('owner/consumer', 'SUCCESS_NO_OP', 0)")
                connection.commit()
            result = remove_macos_launch_agent(repo="owner/consumer", database_path=str(database),
                runtime_config_path=str(root / "runtime.json"),
                label="com.vccp.consumer-dispatcher", plist_path=str(root / "missing.plist"),
                user_id="501", runner=lambda *_args, **_kwargs: SimpleNamespace(
                    returncode=1, stdout="", stderr="Could not find service"))
            self.assertEqual(result["status"], "already_removed", result)
            self.assertTrue(result["runtime_lock_released"])

    def test_remove_blocks_on_active_reviewer(self):
        with tempfile.TemporaryDirectory() as root:
            database = Path(root) / "state.sqlite"
            import sqlite3
            with closing(sqlite3.connect(database)) as connection:
                connection.execute("CREATE TABLE reviewer_executions(repo TEXT, state TEXT)")
                connection.execute("INSERT INTO reviewer_executions VALUES('owner/consumer', 'RUNNING')")
                connection.commit()
            result = remove_macos_launch_agent(repo="owner/consumer", database_path=str(database),
                runtime_config_path=str(Path(root) / "runtime.json"),
                label="com.vccp.consumer-dispatcher", plist_path=str(Path(root) / "missing.plist"),
                user_id="501", runner=lambda *_args, **_kwargs: self.fail("must block before host mutation"))
            self.assertEqual(result["status"], "blocked")
            self.assertIn("Local Reviewer", result["reason"])

    def test_remove_of_already_absent_launcher_is_idempotent(self):
        with tempfile.TemporaryDirectory() as root:
            calls = []
            result = remove_macos_launch_agent(repo="owner/consumer", database_path=str(Path(root) / "state.sqlite"),
                runtime_config_path=str(Path(root) / "runtime.json"),
                label="com.vccp.consumer-dispatcher", plist_path=str(Path(root) / "missing.plist"),
                user_id="501", runner=lambda command, **_kwargs: calls.append(command) or SimpleNamespace(returncode=1, stdout="", stderr="Could not find service"))
            self.assertEqual(result["status"], "already_removed")
            self.assertTrue(result["runtime_lock_released"])
            self.assertFalse(any("bootout" in call for call in calls))

    def test_remove_refuses_symlink_launcher(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            target = root / "user-job.plist"
            target.write_bytes(plistlib.dumps({"Label": "user.job"}))
            alias = root / "com.vccp.consumer-dispatcher.plist"
            alias.symlink_to(target)
            result = remove_macos_launch_agent(repo="owner/consumer", database_path=str(root / "state.sqlite"),
                runtime_config_path=str(root / "runtime.json"), label="com.vccp.consumer-dispatcher",
                plist_path=str(alias), user_id="501",
                runner=lambda *_args, **_kwargs: SimpleNamespace(returncode=1, stdout="", stderr="Could not find service"))
            self.assertEqual(result["status"], "blocked")
            self.assertTrue(alias.is_symlink())
            self.assertTrue(target.exists())

    def test_remove_rechecks_active_work_after_runner_stop_before_deleting_launcher(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            database = root / "state.sqlite"
            import sqlite3
            with closing(sqlite3.connect(database)) as connection:
                connection.execute("CREATE TABLE attempts(repo TEXT, phase TEXT, closeout_pending INTEGER)")
                connection.commit()
            plist = root / "runner.plist"
            plist.write_bytes(plistlib.dumps({"Label": "com.vccp.consumer-dispatcher",
                "ProgramArguments": ["python", "-m", "vccp_runtime.runner", "--runtime-config",
                    str((root / "runtime.json").resolve()), "--repo", "owner/consumer"]}))
            printed = iter(["pid = 42", ""])
            ps_checks = iter([SimpleNamespace(returncode=0, stdout="python -m vccp_runtime.runner"),
                              SimpleNamespace(returncode=1, stdout="")])
            def command(args, **kwargs):
                if args[:2] == ["launchctl", "print"]:
                    value = next(printed)
                    return SimpleNamespace(returncode=0 if "pid =" in value else 1, stdout=value,
                        stderr="" if "pid =" in value else "Could not find service")
                if args[:2] == ["ps", "-p"]:
                    return next(ps_checks)
                if args[:2] == ["launchctl", "bootout"]:
                    with closing(sqlite3.connect(database)) as connection:
                        connection.execute("INSERT INTO attempts VALUES('owner/consumer', 'PR_BOUND', 0)")
                        connection.commit()
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            result = remove_macos_launch_agent(repo="owner/consumer", database_path=str(database),
                runtime_config_path=str(root / "runtime.json"), label="com.vccp.consumer-dispatcher",
                plist_path=str(plist), user_id="501", runner=command)
            self.assertEqual(result["status"], "blocked")
            self.assertIn("active Consumer execution", result["reason"])
            self.assertTrue(plist.exists())

    def test_aligned_running_agent_is_noop_without_restart(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            runtime = root / "runtime"
            runtime.mkdir()
            manifest = root / "manifest.yml"
            config = root / "runtime.json"
            manifest.write_text("manifest\n")
            config.write_text("{}\n")
            plist = root / "com.vccp.consumer-dispatcher.plist"
            args = ["/usr/bin/python3", "-u", "-m", "vccp_runtime.runner", "--manifest",
                    str((root / "manifest.yml").resolve()), "--runtime-config", str((root / "runtime.json").resolve()),
                    "--repo", "owner/consumer", "--continuous", "--poll-interval", "30"]
            plist.write_bytes(plistlib.dumps({"Label": "com.vccp.consumer-dispatcher",
                "ProgramArguments": args, "WorkingDirectory": str(runtime.resolve()),
                "EnvironmentVariables": {"PYTHONPATH": str(runtime.resolve())},
                "VCCPManifestSHA256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
                "VCCPRuntimeConfigSHA256": _runtime_config_digest(config)}))
            calls = []
            def command(command, **kwargs):
                calls.append(command)
                if command[:2] == ["launchctl", "print"]:
                    return SimpleNamespace(returncode=0, stdout="state = running\npid = 34")
                return SimpleNamespace(returncode=0, stdout="python -m vccp_runtime.runner")
            result = reconcile_macos_launch_agent(
                repo="owner/consumer", runtime_root=str(runtime), manifest_path=str(manifest),
                runtime_config_path=str(config), database_path=str(root / "state.sqlite"),
                python_executable="/usr/bin/python3", label="com.vccp.consumer-dispatcher",
                plist_path=str(plist), user_id="501", readiness=lambda: self.fail("no-op must not acquire a held lock"),
                runner=command, environ={})
            self.assertEqual(result["status"], "running", result)
            self.assertTrue(result["no_op"])
            self.assertFalse(any(call[1] in {"bootout", "bootstrap", "kickstart"} for call in calls if call[0] == "launchctl"))

    def test_reconcile_stops_existing_runner_then_reconfigures_readies_starts_and_verifies(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            runtime = root / "runtime"
            runtime.mkdir()
            (root / "manifest.yml").write_text("manifest\n")
            (root / "runtime.json").write_text("{}\n")
            plist = root / "com.vccp.consumer-dispatcher.plist"
            plist.write_bytes(plistlib.dumps({"Label": "com.vccp.consumer-dispatcher",
                "ProgramArguments": ["python3", "-m", "vccp_runtime.runner", "--repo", "owner/consumer"],
                "WorkingDirectory": str(root / "old-runtime")}))
            calls = []

            # Return a loaded process on first/final observation, empty after bootout.
            printed = iter(["state = running\npid = 12", "", "state = running\npid = 34"])
            def command(args, **kwargs):
                calls.append((args, kwargs))
                if args[:2] == ["launchctl", "print"]:
                    output = next(printed)
                    return SimpleNamespace(returncode=0 if "pid =" in output else 1, stdout=output)
                if args[:2] == ["ps", "-p"]:
                    return SimpleNamespace(returncode=0, stdout="python -m vccp_runtime.runner")
                return SimpleNamespace(returncode=0, stdout="")

            events = []
            result = reconcile_macos_launch_agent(
                repo="owner/consumer", runtime_root=str(runtime),
                manifest_path=str(root / "manifest.yml"), runtime_config_path=str(root / "runtime.json"),
                database_path=str(root / "state.sqlite"), python_executable="/usr/bin/python3",
                label="com.vccp.consumer-dispatcher", plist_path=str(plist), user_id="501",
                readiness=lambda: events.append("ready") or {"status": "ready"},
                runner=command, environ={"PATH": "/usr/bin:/bin"})

            configured = plistlib.loads(plist.read_bytes())
            self.assertEqual(result["status"], "running")
            self.assertEqual(result["pid"], 34)
            self.assertEqual(configured["WorkingDirectory"], str(runtime.resolve()))
            self.assertEqual(events, ["ready"])
            names = [call[0][1] if call[0][0] == "launchctl" else call[0][0] for call in calls]
            self.assertLess(names.index("bootout"), names.index("bootstrap"))

    def test_readiness_block_restores_the_old_owner_without_starting_new_runtime(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            runtime = root / "runtime"
            runtime.mkdir()
            (root / "m.yml").write_text("manifest\n")
            (root / "r.json").write_text("{}\n")
            plist = root / "runner.plist"
            plist.write_bytes(plistlib.dumps({"Label": "com.vccp.test-dispatcher",
                "ProgramArguments": ["python", "-m", "vccp_runtime.runner", "--repo", "owner/repo"]}))
            printed = iter(["pid = 12", "", "", "pid = 34"])
            calls = []
            def runner(args, **kwargs):
                calls.append(args)
                if args[:2] == ["launchctl", "print"]:
                    value = next(printed)
                    return SimpleNamespace(returncode=0 if "pid =" in value else 1, stdout=value)
                if args[:2] == ["ps", "-p"]:
                    return SimpleNamespace(returncode=0, stdout="python -m vccp_runtime.runner")
                return SimpleNamespace(returncode=0, stdout="")
            result = reconcile_macos_launch_agent(
                repo="owner/repo", runtime_root=str(runtime), manifest_path=str(root / "m.yml"),
                runtime_config_path=str(root / "r.json"), database_path=str(root / "db"),
                python_executable="python", label="com.vccp.test-dispatcher", plist_path=str(plist),
                user_id="501", readiness=lambda: {"status": "not_ready"}, runner=runner, environ={})
            self.assertEqual(result["status"], "blocked")
            self.assertTrue(result["previous_runner_restored"])
            self.assertIn("bootstrap", [call[1] for call in calls if call[0] == "launchctl"])


if __name__ == "__main__":
    unittest.main()
