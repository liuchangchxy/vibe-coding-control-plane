import plistlib
import hashlib
import json
from contextlib import closing
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from vccp_runtime.host_deployment import (reconcile_macos_launch_agent, remove_macos_launch_agent,
                                          reconcile_windows_sidecar, remove_windows_sidecar)


class HostDeploymentTests(unittest.TestCase):
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
                "VCCPRuntimeConfigSHA256": hashlib.sha256(config.read_bytes()).hexdigest()}))
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

    def test_readiness_block_stops_old_owner_without_starting_new_one(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            runtime = root / "runtime"
            runtime.mkdir()
            (root / "m.yml").write_text("manifest\n")
            (root / "r.json").write_text("{}\n")
            plist = root / "runner.plist"
            plist.write_bytes(plistlib.dumps({"Label": "com.vccp.test-dispatcher",
                "ProgramArguments": ["python", "-m", "vccp_runtime.runner", "--repo", "owner/repo"]}))
            printed = iter(["pid = 12", ""])
            calls = []
            def runner(args, **kwargs):
                calls.append(args)
                if args[:2] == ["launchctl", "print"]:
                    value = next(printed)
                    return SimpleNamespace(returncode=0 if "pid =" in value else 1, stdout=value)
                return SimpleNamespace(returncode=0, stdout="")
            result = reconcile_macos_launch_agent(
                repo="owner/repo", runtime_root=str(runtime), manifest_path=str(root / "m.yml"),
                runtime_config_path=str(root / "r.json"), database_path=str(root / "db"),
                python_executable="python", label="com.vccp.test-dispatcher", plist_path=str(plist),
                user_id="501", readiness=lambda: {"status": "not_ready"}, runner=runner, environ={})
            self.assertEqual(result["status"], "blocked")
            self.assertNotIn("bootstrap", [call[1] for call in calls if call[0] == "launchctl"])

    def test_reconcile_windows_sidecar_creates_definition(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            runtime = root / "runtime"
            runtime.mkdir()
            manifest = root / "manifest.json"
            manifest.write_text("{}", encoding="utf-8")
            config = root / "runtime.json"
            config.write_text("{}", encoding="utf-8")
            sidecar = root / "sidecar.json"
            calls = []
            def runner(args, **kwargs):
                calls.append(args)
                return SimpleNamespace(returncode=1, stdout="", stderr="")

            result = reconcile_windows_sidecar(
                repo="owner/repo", runtime_root=str(runtime),
                manifest_path=str(manifest), runtime_config_path=str(config),
                database_path=str(root / "state.sqlite"), python_executable=r"C:\python\python.exe",
                sidecar_path=str(sidecar), readiness=lambda: {"status": "ready"}, runner=runner
            )
            self.assertEqual(result["status"], "running")
            self.assertEqual(result["mechanism"], "windows_sidecar")
            self.assertTrue(sidecar.is_file())
            content = json.loads(sidecar.read_text(encoding="utf-8"))
            self.assertEqual(content["command"], "C:/python/python.exe")
            self.assertIn("--repo", content["args"])
            self.assertEqual(content["env"]["PYTHONPATH"], runtime.resolve().as_posix())

    def test_remove_windows_sidecar_removes_proven_sidecar(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            config = root / "runtime.json"
            config.write_text("{}", encoding="utf-8")
            sidecar = root / "sidecar.json"
            sidecar.write_text(json.dumps({
                "args": ["-m", "vccp_runtime.runner", "--runtime-config", str(config.resolve()),
                         "--repo", "owner/repo"]
            }), encoding="utf-8")
            def runner(args, **kwargs):
                return SimpleNamespace(returncode=1, stdout="", stderr="")

            result = remove_windows_sidecar(
                repo="owner/repo", database_path=str(root / "state.sqlite"),
                runtime_config_path=str(config), sidecar_path=str(sidecar), runner=runner
            )
            self.assertEqual(result["status"], "removed")
            self.assertFalse(sidecar.exists())


if __name__ == "__main__":
    unittest.main()
