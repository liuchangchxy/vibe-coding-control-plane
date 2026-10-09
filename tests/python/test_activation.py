import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from vccp_runtime.activation import qualify_activation


MANIFEST = {
    "schema_version": 2,
    "issue_contract": {"max_automated_repairs": 3,
                       "active_coordination_labels": ["agent-ready", "agent-working", "changes-requested"],
                       "terminal_coordination_labels": ["infra-blocked", "needs-human"],
                       "frozen_spec_label": "frozen-spec"},
    "repository": {"base_branch": "main", "implementer_authors": ["builder[bot]"]},
    "reviewer": {"required_checks": [{"name": "Unit Tests", "accepted_conclusions": ["success"]}]},
}
REPO = "example/consumer"


class FakeProvider:
    api_url = "https://api.github.com"
    target_repository = REPO

    def __init__(self, result=None):
        self.result = result or {"installed": True, "permissions_sufficient": True,
                                 "missing_permissions": []}
        self.probes = []
    def probe(self, repo):
        self.probes.append(repo)
        return self.result


class FakeAPI:
    def __init__(self, repo=REPO): self.repo = repo; self.reads = []
    def get(self, path):
        self.reads.append(path)
        return {"full_name": self.repo}


class FakeWriter:
    def replace_labels(self, *args): raise AssertionError("readiness must not mutate labels")


class FakePushBackend:
    def push(self, *args, **kwargs): raise AssertionError("readiness must not push")


class ActivationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        subprocess.run(["git", "init", str(self.workspace)], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(self.workspace), "remote", "add", "origin",
                        f"https://github.com/{REPO}.git"], check=True, capture_output=True)
        self.executable = self.root / "language_server.exe"
        self.executable.write_bytes(b"fake executable")
        self.credential_file = self.root / "app-key.dpapi"
        self.credential_file.write_bytes(b"fake ciphertext")
        (self.root / "state").mkdir()
        self.config = {
            "database_path": str(self.root / "state" / "runtime.sqlite"),
            "workspace": str(self.workspace),
            "target_repository": REPO,
            "enrollment": {"authorized_repository": REPO},
            "owner_id": "test-owner",
            "antigravity_executable": str(self.executable),
            "github_app": {"app_id": "123456", "expected_app_slug": "app",
                           "credential_source": {"type": "windows_dpapi_file",
                                                 "path": str(self.credential_file)}},
        }
        self.provider, self.api, self.writer, self.push_backend = (
            FakeProvider(), FakeAPI(), FakeWriter(), FakePushBackend())

    def tearDown(self): self.temp.cleanup()

    def _qualify(self, config=None, provider=None, api=None):
        with patch("vccp_runtime.activation.DPAPIFileCredentialSource") as source:
            source.return_value.load_private_key.return_value = object()
            return qualify_activation(
                MANIFEST, self.config if config is None else config, REPO,
                credential_provider=self.provider if provider is None else provider,
                api=self.api if api is None else api, writer=self.writer,
                push_backend=self.push_backend,
            )

    def test_readiness_probes_app_reads_repository_and_constructs_without_legacy_helpers(self):
        result = self._qualify()
        self.assertEqual(result["status"], "ready")
        self.assertEqual(self.provider.probes, [REPO])
        self.assertEqual(self.api.reads, [f"/repos/{REPO}"])
        self.assertEqual(result["credential_source"], {"type": "windows_dpapi_file", "present": True})
        self.assertFalse(Path(self.config["database_path"]).exists())
        self.assertEqual(list((self.root / "state").iterdir()), [])
        self.assertNotIn("github_read_token_env", self.config)
        self.assertNotIn("app_gh_executable", self.config)
        self.assertNotIn("app_git_push_executable", self.config)

    def test_missing_or_insufficient_installation_reports_human_action(self):
        provider = FakeProvider({"installed": False, "permissions_sufficient": False,
                                 "missing_permissions": []})
        result = self._qualify(provider=provider)
        self.assertEqual(result["status"], "not_ready")
        self.assertIn("human must install", result["blockers"][0])

        provider = FakeProvider({"installed": True, "permissions_sufficient": False,
                                 "missing_permissions": {"contents": "write"}})
        result = self._qualify(provider=provider)
        self.assertEqual(result["status"], "not_ready")
        self.assertEqual(result["installation"]["missing_permissions"], {"contents": "write"})
        self.assertIn("contents:write", result["blockers"][0])

    def test_compatible_repository_without_enrollment_and_target_mismatch_are_not_ready(self):
        un_enrolled = dict(self.config)
        un_enrolled.pop("enrollment")
        result = self._qualify(config=un_enrolled)
        self.assertEqual(result["status"], "not_ready")
        self.assertTrue(any("human enrollment authorization is required" in item for item in result["blockers"]))
        wrong_target = dict(self.config, target_repository="other/repo")
        result = self._qualify(config=wrong_target)
        self.assertEqual(result["status"], "not_ready")
        self.assertTrue(any("target_repository" in item for item in result["blockers"]))

    def test_repository_read_mismatch_and_missing_credential_fail_closed(self):
        result = self._qualify(api=FakeAPI("other/repo"))
        self.assertEqual(result["status"], "not_ready")
        self.assertIn("wrong target", result["blockers"][0])
        config = dict(self.config)
        config["github_app"] = dict(self.config["github_app"], credential_source={
            "type": "windows_dpapi_file", "path": str(self.root / "missing.dpapi")})
        result = self._qualify(config=config)
        self.assertEqual(result["status"], "not_ready")
        self.assertIn("credential source is unavailable", result["blockers"][0])

    def test_secret_never_appears_in_readiness_result(self):
        secret = "fake-secret-should-not-be-in-report"
        result = self._qualify()
        self.assertNotIn(secret, json.dumps(result))


if __name__ == "__main__": unittest.main()
