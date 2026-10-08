import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from vccp_runtime.activation import qualify_activation


MANIFEST = {
    "schema_version": 2,
    "issue_contract": {"max_automated_repairs": 3},
    "repository": {"base_branch": "main", "implementer_authors": ["builder[bot]"]},
    "reviewer": {"required_checks": [{"name": "Unit Tests", "accepted_conclusions": ["success"]}]},
}
REPO = "example/consumer"
SECRET = "should-never-appear-in-output"


class ActivationTests(unittest.TestCase):
    def _environment(self, root: Path, remote=REPO):
        workspace = root / "workspace"
        workspace.mkdir()
        subprocess.run(["git", "init", str(workspace)], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(workspace), "remote", "add", "origin",
                        f"https://github.com/{remote}.git"], check=True, capture_output=True)
        executables = []
        for name in ("language_server.exe", "app-gh.exe", "app-git-push.exe"):
            executable = root / name
            executable.write_text("fake", encoding="utf-8")
            executables.append(str(executable))
        config = {
            "database_path": str(root / "state" / "runtime.sqlite"),
            "workspace": str(workspace),
            "owner_id": "test-owner",
            "antigravity_executable": executables[0],
            "app_gh_executable": executables[1],
            "app_git_push_executable": executables[2],
            "github_read_token_env": "VCCP_TEST_READ_TOKEN",
        }
        (root / "state").mkdir()
        return config

    def test_complete_config_constructs_existing_runtime_without_dispatch_or_database_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self._environment(root)
            result = qualify_activation(MANIFEST, config, REPO, {"VCCP_TEST_READ_TOKEN": SECRET})
            encoded = json.dumps(result)
            self.assertEqual(result["status"], "ready")
            self.assertEqual(result["credential_source"], {
                "environment_variable": "VCCP_TEST_READ_TOKEN", "present": True,
            })
            self.assertNotIn(SECRET, encoded)
            self.assertFalse(Path(config["database_path"]).exists())
            self.assertEqual(list((root / "state").iterdir()), [])

    def test_incomplete_config_workspace_mismatch_and_missing_credential_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self._environment(root, remote="other/repo")
            config.pop("owner_id")
            result = qualify_activation(MANIFEST, config, REPO, {})
            self.assertEqual(result["status"], "not_ready")
            self.assertTrue(any("owner_id" in blocker for blocker in result["blockers"]))

            config["owner_id"] = "test-owner"
            result = qualify_activation(MANIFEST, config, REPO, {})
            self.assertEqual(result["status"], "not_ready")
            self.assertTrue(any("workspace origin" in blocker for blocker in result["blockers"]))
            self.assertTrue(any("present: false" in blocker for blocker in result["blockers"]))
            self.assertNotIn(SECRET, json.dumps(result))

    def test_missing_executable_and_invalid_contract_are_not_ready(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self._environment(root)
            config["app_git_push_executable"] = str(root / "missing.exe")
            result = qualify_activation({"schema_version": 1}, config, REPO, {"VCCP_TEST_READ_TOKEN": SECRET})
            self.assertEqual(result["status"], "not_ready")
            self.assertTrue(any("schema_version 2" in blocker for blocker in result["blockers"]))
            self.assertTrue(any("app_git_push_executable" in blocker for blocker in result["blockers"]))


if __name__ == "__main__":
    unittest.main()
