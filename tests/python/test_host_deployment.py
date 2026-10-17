import plistlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from vccp_runtime.host_deployment import reconcile_macos_launch_agent


class HostDeploymentTests(unittest.TestCase):
    def test_reconcile_stops_existing_runner_then_reconfigures_readies_starts_and_verifies(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            runtime = root / "runtime"
            runtime.mkdir()
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


if __name__ == "__main__":
    unittest.main()
