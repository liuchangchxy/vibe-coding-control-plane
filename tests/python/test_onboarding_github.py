import unittest

from vccp_runtime.onboarding_github import dispatch


class Provider:
    def __init__(self):
        self.calls = []
    def request(self, method, path, repo, payload=None, **kwargs):
        self.calls.append((method, path, repo, payload, kwargs))
        return 200, {"full_name": repo}


class OnboardingGitHubTests(unittest.TestCase):
    def test_dispatch_only_exposes_the_onboarding_read_and_label_operations(self):
        provider = Provider()
        result = dispatch({"action": "getRepository", "repo": "owner/repo"}, provider=provider)
        self.assertEqual(result["full_name"], "owner/repo")
        self.assertEqual(provider.calls[0][:3], ("GET", "/repos/owner/repo", "owner/repo"))
        with self.assertRaises(ValueError):
            dispatch({"action": "deleteRepository", "repo": "owner/repo"}, provider=provider)

    def test_workflow_tree_requests_only_additional_workflows_permission(self):
        provider = Provider()
        dispatch({"action": "createTree", "repo": "owner/repo", "tree": {
            "tree": [{"path": ".github/workflows/managed.yml", "mode": "100644", "type": "blob", "sha": "abc"}],
        }}, provider=provider)

        self.assertEqual(provider.calls[0][4], {"additional_permissions": {"workflows": "write"}})

    def test_non_workflow_tree_uses_default_permission_set(self):
        provider = Provider()
        dispatch({"action": "createTree", "repo": "owner/repo", "tree": {
            "tree": [{"path": "README.md", "mode": "100644", "type": "blob", "sha": "abc"}],
        }}, provider=provider)

        self.assertEqual(provider.calls[0][4], {})


if __name__ == "__main__":
    unittest.main()
