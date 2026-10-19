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


if __name__ == "__main__":
    unittest.main()
