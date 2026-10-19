import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from vccp_runtime.deploy import deploy_host


class DeployHostTests(unittest.TestCase):
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

    def test_deploy_windows_dispatches_to_windows_sidecar(self):
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
                      "dispatcher": {"sidecar_path": str(root / "sidecar.json")}}
            result = deploy_host(repo="owner/repo", manifest={"vccp": {"revision": revision}},
                local_config=config, manifest_path=str(root / "manifest.json"), runtime_config_path=str(root / "runtime.json"),
                source_root=str(source), runtimes_root=str(root / "runtimes"), platform="win32",
                readiness=lambda runtime: {"status": "ready"}, host_reconciler=lambda **kwargs: events.append(kwargs) or {"status": "running"})
            self.assertEqual(result["status"], "running")
            self.assertEqual(events[0]["sidecar_path"], str(root / "sidecar.json"))
            self.assertEqual(events[0]["runtime_root"], str((root / "runtimes" / revision).resolve()))
            self.assertEqual(result["revision"], revision)

    def test_remove_windows_dispatches_to_windows_sidecar(self):
        from vccp_runtime.deploy import remove_host
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            config = {"database_path": str(root / "state.sqlite"), "owner_id": "owner",
                      "dispatcher": {"sidecar_path": str(root / "sidecar.json")}}
            with patch("vccp_runtime.deploy.remove_windows_sidecar", return_value={"status": "removed"}):
                result = remove_host(repo="owner/repo", local_config=config,
                                     runtime_config_path=str(root / "runtime.json"), platform="win32")
            self.assertEqual(result["status"], "removed")

    def test_deploy_windows_blocks_on_ambiguous_dual_sidecars(self):
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

            config = {"database_path": str(root / "state.sqlite"), "workspace": str(source), "owner_id": "owner"}
            with patch("vccp_runtime.deploy.Path.home", return_value=root):
                sc1 = root / ".gemini" / "config" / "sidecars" / "easy-exam-vccp" / "sidecar.json"
                sc2 = root / ".gemini" / "config" / "sidecars" / "easyexam-vccp" / "sidecar.json"
                sc1.parent.mkdir(parents=True)
                sc2.parent.mkdir(parents=True)
                sc1.write_text("{}", encoding="utf-8")
                sc2.write_text("{}", encoding="utf-8")

                result = deploy_host(repo="liuchangchxy/easy-exam", manifest={"vccp": {"revision": revision}},
                    local_config=config, manifest_path=str(root / "manifest.json"),
                    runtime_config_path=str(root / "runtime.json"), source_root=str(source),
                    runtimes_root=str(root / "runtimes"), platform="win32",
                    readiness=lambda runtime: {"status": "ready"})
            self.assertEqual(result["status"], "blocked")
            self.assertIn("ambiguous multiple Windows Sidecar", result["reason"])


if __name__ == "__main__":
    unittest.main()
