import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from vccp_runtime.deploy import _macos_dispatcher_label, deploy_host
from vccp_runtime.host_deployment import _runtime_config_digest


class DeployHostTests(unittest.TestCase):
    def test_default_macos_label_includes_owner_and_has_collision_resistant_suffix(self):
        first = _macos_dispatcher_label("owner-one/app")
        second = _macos_dispatcher_label("owner-two/app")
        self.assertNotEqual(first, second)
        self.assertEqual(first, _macos_dispatcher_label("owner-one/app"))

    def test_upgrade_target_does_not_change_runtime_config_digest(self):
        with tempfile.TemporaryDirectory() as folder:
            config = Path(folder) / "runtime.json"
            config.write_text('{"target_repository":"owner/repo","upgrade_target":{"revision":"abc"}}', encoding="utf-8")
            before = _runtime_config_digest(config)
            config.write_text('{"target_repository":"owner/repo"}', encoding="utf-8")
            self.assertEqual(_runtime_config_digest(config), before)

    def test_deploy_prepares_exact_revision_before_host_reconcile(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "source"
            source.mkdir()
            import subprocess
            subprocess.run(["git", "init", "-q", str(source)], check=True)
            subprocess.run(["git", "-C", str(source), "config", "user.email", "test@example.com"], check=True)
            subprocess.run(["git", "-C", str(source), "config", "user.name", "Test"], check=True)
            (source / "runtime.py").write_text("runtime\n")
            subprocess.run(["git", "-C", str(source), "add", "."], check=True)
            subprocess.run(["git", "-C", str(source), "commit", "-qm", "runtime"], check=True)
            revision = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
            events = []
            config = {"database_path": str(root / "state.sqlite"), "workspace": str(source), "owner_id": "owner",
                      "dispatcher": {"label": "com.vccp.test-dispatcher", "plist_path": str(root / "agent.plist")}}
            with patch("vccp_runtime.deploy.os.getuid", return_value=501, create=True):
                result = deploy_host(repo="owner/repo", manifest={"vccp": {"revision": revision}},
                    local_config=config, manifest_path=str(root / "manifest.yml"), runtime_config_path=str(root / "runtime.json"),
                    source_root=str(source), runtimes_root=str(root / "runtimes"), platform="darwin",
                    readiness=lambda runtime: {"status": "ready"}, host_reconciler=lambda **kwargs: events.append(kwargs) or {"status": "running"})
            self.assertEqual(result["status"], "running")
            self.assertEqual(events[0]["runtime_root"], str((root / "runtimes" / revision).resolve()))
            self.assertEqual(result["revision"], revision)

    def test_old_controller_hands_deployment_to_the_exact_pinned_runtime(self):
        import subprocess
        import json
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder)
            subprocess.run(["git", "init", "-q", str(source)], check=True)
            subprocess.run(["git", "-C", str(source), "config", "user.email", "test@example.com"], check=True)
            subprocess.run(["git", "-C", str(source), "config", "user.name", "Test"], check=True)
            (source / "runtime.py").write_text("controller A\n")
            subprocess.run(["git", "-C", str(source), "add", "."], check=True)
            subprocess.run(["git", "-C", str(source), "commit", "-qm", "controller A"], check=True)
            pinned_revision = "b" * 40
            calls = []
            def runner(command, **kwargs):
                calls.append((command, kwargs))
                return SimpleNamespace(returncode=0, stdout=json.dumps({"status": "running"}))
            with patch("vccp_runtime.deploy.ensure_runtime_checkout", return_value=source):
                result = deploy_host(repo="owner/repo", manifest={"vccp": {"revision": pinned_revision}},
                    local_config={}, manifest_path=str(source / "manifest.yml"),
                    runtime_config_path=str(source / "runtime.json"), source_root=str(source),
                    runtimes_root=str(source / "runtimes"), runner=runner)
            self.assertEqual(result["status"], "running")
            self.assertEqual(calls[0][0][1:], ["-m", "vccp_runtime.deploy"])
            self.assertEqual(calls[0][1]["cwd"], str(source))


if __name__ == "__main__":
    unittest.main()
