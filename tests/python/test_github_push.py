import subprocess
import tempfile
import unittest
import base64
from pathlib import Path
from types import SimpleNamespace

from vccp_runtime.github_app import GitHubAppIdentityError
from vccp_runtime.github_push import GitHubAppPushBackend


REPO = "owner/consumer"
TOKEN = "fake-push-token-that-must-not-escape"


def run_git(args, cwd=None, **kwargs):
    kwargs.setdefault("text", True)
    kwargs.setdefault("capture_output", True)
    kwargs.setdefault("check", False)
    return subprocess.run(args, cwd=cwd, **kwargs)


def init_worktree(root: Path):
    main = root / "main"
    main.mkdir()
    run_git(["git", "init", "-b", "main"], main).check_returncode()
    run_git(["git", "config", "user.name", "VCCP Test"], main).check_returncode()
    run_git(["git", "config", "user.email", "vccp@example.invalid"], main).check_returncode()
    (main / "file.txt").write_text("base", encoding="utf-8")
    run_git(["git", "add", "file.txt"], main).check_returncode()
    run_git(["git", "commit", "-m", "base"], main).check_returncode()
    feature = root / "feature-worktree"
    run_git(["git", "worktree", "add", "-b", "feature/test", str(feature), "main"], main).check_returncode()
    run_git(["git", "remote", "add", "origin", f"https://github.com/{REPO}.git"], feature).check_returncode()
    sha = run_git(["git", "rev-parse", "refs/heads/feature/test"], feature).stdout.strip()
    return main, feature, sha


class FakeProvider:
    api_url = "https://api.github.com"
    target_repository = REPO

    def __init__(self, remote_sha=None):
        self.remote_sha = remote_sha
        self.requests = []

    def get_token(self, repo):
        if repo.casefold() != REPO:
            raise GitHubAppIdentityError("target denied")
        return TOKEN

    def request(self, method, path, repo, payload=None):
        self.requests.append((method, path, repo, payload))
        return 200, {"object": {"sha": self.remote_sha}}


class PushBackendTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        _, self.workspace, self.sha = init_worktree(self.root)
        self.provider = FakeProvider(self.sha)
        self.push_calls = []

        def runner(args, **kwargs):
            if args[1] == "push":
                self.push_calls.append((list(args), kwargs))
                return SimpleNamespace(returncode=0, stdout=f"safe-output-{TOKEN}", stderr="")
            return run_git(args, **kwargs)

        self.backend = GitHubAppPushBackend(self.provider, self.workspace, REPO, "main", runner=runner)

    def tearDown(self):
        self.temp.cleanup()

    def test_successful_push_supports_linked_worktree_and_verifies_remote_sha(self):
        result = self.backend.push(REPO, "feature/test", self.sha, "feature/test", controlled=True)
        self.assertEqual(result, {"status": "pushed", "repository": REPO,
                                  "destination_ref": "refs/heads/feature/test", "sha": self.sha})
        args, kwargs = self.push_calls[0]
        self.assertNotIn(TOKEN, " ".join(args))
        git_config = " ".join(value for key, value in kwargs["env"].items()
                               if key.startswith("GIT_CONFIG_VALUE_"))
        self.assertIn(f"https://github.com/{REPO}.git", git_config)
        self.assertIn(base64.b64encode(f"x-access-token:{TOKEN}".encode()).decode(), git_config)
        self.assertNotIn(TOKEN, git_config)
        self.assertNotIn(TOKEN, repr(result))
        self.assertEqual(self.provider.requests[-1][1],
                         f"/repos/{REPO}/git/ref/heads/feature/test")

    def test_expected_sha_mismatch_and_wrong_repo_fail_before_push(self):
        with self.assertRaisesRegex(GitHubAppIdentityError, "expected SHA"):
            self.backend.push(REPO, "feature/test", "0" * 40, "feature/test", controlled=True)
        with self.assertRaisesRegex(GitHubAppIdentityError, "not authorized"):
            self.backend.push("owner/other", "feature/test", self.sha, "feature/test", controlled=True)
        self.assertEqual(self.push_calls, [])

    def test_remote_mismatch_and_invalid_destination_fail_closed(self):
        run_git(["git", "remote", "set-url", "origin", "https://github.com/owner/other.git"], self.workspace).check_returncode()
        with self.assertRaisesRegex(GitHubAppIdentityError, "Git remote"):
            self.backend.push(REPO, "feature/test", self.sha, "feature/test", controlled=True)
        run_git(["git", "remote", "set-url", "origin", f"https://github.com/{REPO}.git"], self.workspace).check_returncode()
        with self.assertRaisesRegex(GitHubAppIdentityError, "destination ref"):
            self.backend.push(REPO, "feature/test", self.sha, "main", controlled=True)
        self.assertEqual(self.push_calls, [])

    def test_remote_sha_verification_failure_has_no_credential_in_result(self):
        self.provider.remote_sha = "f" * 40
        with self.assertRaisesRegex(Exception, "remote ref verification failed") as captured:
            self.backend.push(REPO, "feature/test", self.sha, "feature/test", controlled=True)
        self.assertNotIn(TOKEN, str(captured.exception))

    def test_push_requires_runtime_controlled_wrapper(self):
        with self.assertRaisesRegex(GitHubAppIdentityError, "runtime-controlled wrapper"):
            self.backend.push(REPO, "feature/test", self.sha, "feature/test")
        self.assertEqual(self.push_calls, [])


if __name__ == "__main__":
    unittest.main()
