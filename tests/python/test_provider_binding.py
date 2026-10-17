import unittest

from vccp_runtime.adapters import _configured_providers_for_role


class DeterministicProviderBindingTests(unittest.TestCase):
    def setUp(self):
        self.config = {
            "providers": [
                {"key": "antigravity", "type": "antigravity", "executable": "/bin/agy"},
                {"key": "claude_code", "type": "claude_code", "executable": "/bin/claude"},
            ]
        }

    def test_explicit_antigravity_binding_never_routes_to_claude(self):
        config = {**self.config, "implementer_provider": "antigravity"}

        self.assertEqual(_configured_providers_for_role(config, "implementer"), [self.config["providers"][0]])

    def test_explicit_claude_binding_never_routes_to_antigravity(self):
        config = {**self.config, "implementer_provider": "claude_code"}

        self.assertEqual(_configured_providers_for_role(config, "implementer"), [self.config["providers"][1]])

    def test_reviewer_binding_uses_the_explicit_machine_local_provider(self):
        config = {**self.config, "reviewer_provider": "claude_code"}

        self.assertEqual(_configured_providers_for_role(config, "reviewer"), [self.config["providers"][1]])

    def test_unknown_role_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "provider role must be implementer or reviewer"):
            _configured_providers_for_role(self.config, "dispatcher")

    def test_missing_explicit_binding_fails_closed(self):
        config = {**self.config, "implementer_provider": "missing"}

        with self.assertRaisesRegex(ValueError, "does not name a configured provider"):
            _configured_providers_for_role(config, "implementer")

    def test_legacy_provider_list_keeps_its_ordered_fallback_behavior(self):
        self.assertEqual(_configured_providers_for_role(self.config, "implementer"), self.config["providers"])


if __name__ == "__main__":
    unittest.main()
