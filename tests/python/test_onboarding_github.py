import unittest

from vccp_runtime.onboarding_github import dispatch


class Provider:
    def __init__(self):
        self.calls = []
    def request(self, method, path, repo, payload=None):
        self.calls.append((method, path, repo, payload))
        return 200, {"full_name": repo}


class OnboardingGitHubTests(unittest.TestCase):
    def test_dispatch_only_exposes_the_onboarding_read_and_label_operations(self):
        provider = Provider()
        result = dispatch({"action": "getRepository", "repo": "owner/repo"}, provider=provider)
        self.assertEqual(result["full_name"], "owner/repo")
        self.assertEqual(provider.calls[0][:3], ("GET", "/repos/owner/repo", "owner/repo"))
        with self.assertRaises(ValueError):
            dispatch({"action": "deleteRepository", "repo": "owner/repo"}, provider=provider)


if __name__ == "__main__":
    unittest.main()
