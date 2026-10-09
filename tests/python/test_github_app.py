import base64
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding

from vccp_runtime.adapters import GitHubAppWriter
from vccp_runtime.github_app import (DPAPIFileCredentialSource, GitHubAppCredentialProvider,
                                     GitHubAppError, GitHubAppIdentityError,
                                     GitHubAppNotInstalled, GitHubAppPermissionError,
                                     GitHubHTTPError, REQUIRED_PERMISSIONS, create_app_jwt)


APP_ID = "123456"
REPO = "owner/consumer"
APP_SLUG = "consumer-control"
SECRET_TOKEN = "fake-installation-token-never-print"
EXPIRY = "2030-01-01T00:00:00Z"


def decode_segment(segment):
    return json.loads(base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4)))


class FakeSource:
    def __init__(self, key): self.key = key
    def load_private_key(self): return self.key


class FakeTransport:
    def __init__(self, permissions=None, installed=True, token=SECRET_TOKEN):
        self.permissions = dict(permissions or REQUIRED_PERMISSIONS)
        self.installed, self.token = installed, token
        self.calls = []
        self.issue_labels = {"agent-ready"}
        self.token_mints = 0

    def request(self, method, url, token, payload=None):
        self.calls.append((method, url, token, payload))
        if url.endswith("/app"):
            return 200, {"id": int(APP_ID), "slug": APP_SLUG}
        if url.endswith(f"/repos/{REPO}/installation"):
            if not self.installed:
                raise GitHubHTTPError(404, "GET")
            return 200, {"id": 88, "account": {"login": "owner"}, "permissions": self.permissions}
        if url.endswith("/app/installations/88/access_tokens"):
            self.token_mints += 1
            requested = payload["permissions"]
            return 201, {"token": self.token, "expires_at": EXPIRY,
                         "repositories": [{"full_name": REPO}], "permissions": requested}
        if url.endswith(f"/repos/{REPO}"):
            return 200, {"full_name": REPO}
        if url.endswith(f"/repos/{REPO}/issues/7"):
            return 200, {"number": 7, "repository_url": f"https://api.github.com/repos/{REPO}",
                         "labels": [{"name": name} for name in sorted(self.issue_labels)]}
        if url.endswith(f"/repos/{REPO}/issues/7/labels/agent-ready"):
            self.issue_labels.discard("agent-ready")
            return 200, {}
        if url.endswith(f"/repos/{REPO}/issues/7/labels"):
            self.issue_labels.update(payload["labels"])
            return 200, [{"name": name} for name in sorted(self.issue_labels)]
        if url.endswith(f"/repos/{REPO}/pulls"):
            return 201, {"number": 44}
        raise AssertionError((method, url))


class GitHubAppProviderTests(unittest.TestCase):
    def setUp(self):
        self.private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    def provider(self, transport, clock=lambda: 1_800_000_000):
        return GitHubAppCredentialProvider(APP_ID, FakeSource(self.private_key), REPO, APP_SLUG,
                                           transport=transport, clock=clock)

    def test_app_jwt_is_rs256_and_uses_configured_issuer_and_short_lifetime(self):
        issued_at = 1_700_000_000
        jwt = create_app_jwt(APP_ID, self.private_key, issued_at)
        header, payload, signature = jwt.split(".")
        self.assertEqual(decode_segment(header), {"alg": "RS256", "typ": "JWT"})
        claims = decode_segment(payload)
        self.assertEqual(claims, {"iss": APP_ID, "iat": issued_at - 60, "exp": issued_at + 540})
        self.private_key.public_key().verify(
            base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4)),
            f"{header}.{payload}".encode(), padding.PKCS1v15(), hashes.SHA256())

    def test_probe_validates_app_installation_and_mints_repo_scoped_token(self):
        now = [1_800_000_000]
        transport = FakeTransport()
        provider = self.provider(transport, clock=lambda: now[0])
        result = provider.probe(REPO)
        self.assertEqual(result["installed"], True)
        self.assertEqual(result["permissions_sufficient"], True)
        self.assertEqual(result["missing_permissions"], [])
        self.assertEqual(provider.get_token(REPO), SECRET_TOKEN)
        jwt = transport.calls[0][2]
        self.assertEqual(decode_segment(jwt.split(".")[1])["iss"], APP_ID)
        mint = next(call for call in transport.calls if call[0] == "POST")
        self.assertEqual(mint[3], {"repositories": ["consumer"], "permissions": REQUIRED_PERMISSIONS})
        self.assertNotIn(SECRET_TOKEN, repr(result))

    def test_probe_reports_missing_installation_and_permission_without_writes(self):
        transport = FakeTransport(installed=False)
        with self.assertRaises(GitHubAppNotInstalled):
            self.provider(transport).probe(REPO)
        self.assertFalse(any(call[0] == "POST" for call in transport.calls))

        permissions = dict(REQUIRED_PERMISSIONS, contents="read")
        result = self.provider(FakeTransport(permissions=permissions)).probe(REPO)
        self.assertFalse(result["permissions_sufficient"])
        self.assertEqual(result["missing_permissions"], {"contents": "write"})

    def test_repo_fence_and_app_identity_fail_closed(self):
        provider = self.provider(FakeTransport())
        with self.assertRaises(GitHubAppIdentityError):
            provider.get_token("owner/other")
        transport = FakeTransport()
        transport.request = lambda *args, **kwargs: (200, {"id": 8, "slug": APP_SLUG})
        with self.assertRaises(GitHubAppIdentityError):
            self.provider(transport).probe(REPO)

    def test_expiry_refreshes_short_lived_installation_token(self):
        now = [1_800_000_000]
        transport = FakeTransport()
        transport.request = self._token_transport(transport, now)
        provider = self.provider(transport, clock=lambda: now[0])
        first = provider.get_token(REPO)
        self.assertEqual(provider.get_token(REPO), first)
        self.assertEqual(transport.token_mints, 1)
        now[0] = datetime(2030, 1, 1, tzinfo=timezone.utc).timestamp() - 250
        second = provider.get_token(REPO)
        self.assertEqual(second, f"{SECRET_TOKEN}-2")
        self.assertEqual(transport.token_mints, 2)

    @staticmethod
    def _token_transport(transport, now):
        def request(method, url, token, payload=None):
            transport.calls.append((method, url, token, payload))
            if url.endswith("/app"):
                return 200, {"id": int(APP_ID), "slug": APP_SLUG}
            if url.endswith(f"/repos/{REPO}/installation"):
                return 200, {"id": 88, "account": {"login": "owner"}, "permissions": REQUIRED_PERMISSIONS}
            if url.endswith("/app/installations/88/access_tokens"):
                transport.token_mints += 1
                expiry = datetime.fromtimestamp(now[0] + 600, timezone.utc).isoformat().replace("+00:00", "Z")
                return 201, {"token": f"{SECRET_TOKEN}-{transport.token_mints}", "expires_at": expiry,
                             "repositories": [{"full_name": REPO}], "permissions": REQUIRED_PERMISSIONS}
            raise AssertionError(url)
        return request

    def test_invalid_or_empty_protected_credential_has_safe_error(self):
        class BadDPAPI:
            @staticmethod
            def unprotect(value): return b"not-a-key"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "credential.dpapi"
            path.write_bytes(b"ciphertext")
            with self.assertRaisesRegex(GitHubAppError, "not a PEM"):
                DPAPIFileCredentialSource(path, BadDPAPI).load_private_key()


class GitHubAppWriterTests(unittest.TestCase):
    def setUp(self):
        self.transport = FakeTransport()
        provider_tests = GitHubAppProviderTests()
        provider_tests.setUp()
        self.provider = provider_tests.provider(self.transport)
        self.writer = GitHubAppWriter(credential_provider=self.provider, target_repository=REPO,
                                      allowed_labels={"agent-ready", "agent-working", "needs-human"})

    def test_writer_performs_only_fenced_label_transition_and_verifies_result(self):
        self.assertTrue(self.writer.replace_labels(REPO, 7, ["agent-ready"], ["agent-working"]))
        self.assertEqual(self.transport.issue_labels, {"agent-working"})
        methods = [call[0] for call in self.transport.calls if "/issues/7" in call[1]]
        self.assertEqual(methods, ["GET", "DELETE", "POST", "GET"])

    def test_writer_rejects_wrong_repo_and_unsupported_label_before_request(self):
        before = len(self.transport.calls)
        with self.assertRaises(GitHubAppIdentityError):
            self.writer.replace_labels("owner/other", 7, ["agent-ready"], [])
        with self.assertRaises(GitHubAppIdentityError):
            self.writer.replace_labels(REPO, 7, ["ordinary-label"], [])
        self.assertEqual(len(self.transport.calls), before)

    def test_writer_limits_pr_creation_to_enrolled_issue_and_base(self):
        number = self.writer.create_pull_request(REPO, 7, "Change", "Summary\n\nResolves #7", "work", "main")
        self.assertEqual(number, 44)
        request = next(call for call in self.transport.calls if call[0] == "POST" and call[1].endswith("/pulls"))
        self.assertEqual(request[3]["head"], "work")
        with self.assertRaises(GitHubAppIdentityError):
            self.writer.create_pull_request(REPO, 7, "Change", "No issue ref", "work", "main")
        with self.assertRaises(GitHubAppIdentityError):
            self.writer.create_pull_request("owner/other", 7, "Change", "#7", "work", "main")

    def test_writer_accepts_github_closing_keywords_case_insensitively(self):
        bodies = ("Close #7", "Closes #7", "Closed #7", "Fix #7", "Fixes #7", "Fixed #7",
                  "Resolve #7", "Resolves #7", "Resolved #7", "cLoSeD: #7")
        for body in bodies:
            with self.subTest(body=body):
                before = len([call for call in self.transport.calls
                              if call[0] == "POST" and call[1].endswith("/pulls")])
                self.writer.create_pull_request(REPO, 7, "Change", body, "work", "main")
                after = len([call for call in self.transport.calls
                             if call[0] == "POST" and call[1].endswith("/pulls")])
                self.assertEqual(after, before + 1)

    def test_writer_rejects_nonclosing_or_wrong_issue_references_before_mutation(self):
        bodies = ("See #7", "Related to #7", "Refs #7", "Closes #8", "No issue reference")
        for body in bodies:
            with self.subTest(body=body):
                before = len([call for call in self.transport.calls
                              if call[0] == "POST" and call[1].endswith("/pulls")])
                with self.assertRaises(GitHubAppIdentityError):
                    self.writer.create_pull_request(REPO, 7, "Change", body, "work", "main")
                after = len([call for call in self.transport.calls
                             if call[0] == "POST" and call[1].endswith("/pulls")])
                self.assertEqual(after, before)


if __name__ == "__main__":
    unittest.main()
