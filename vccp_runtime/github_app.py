"""Machine-local GitHub App credentials and short-lived installation tokens."""
from __future__ import annotations

import base64
import ctypes
import datetime as dt
import json
import os
from pathlib import Path
import re
import sys
import time
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding


REQUIRED_PERMISSIONS = {
    "contents": "write",
    "issues": "write",
    "pull_requests": "write",
    "checks": "read",
    "metadata": "read",
}
_REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_APPROX_TOKEN_TTL = dt.timedelta(minutes=5)


class GitHubAppError(RuntimeError):
    """Safe, credential-free production error."""


class GitHubAppNotInstalled(GitHubAppError):
    pass


class GitHubAppPermissionError(GitHubAppError):
    def __init__(self, missing_permissions: dict[str, str]):
        self.missing_permissions = dict(sorted(missing_permissions.items()))
        detail = ", ".join(f"{name}:{level}" for name, level in self.missing_permissions.items())
        super().__init__(f"GitHub App installation needs human-approved permissions: {detail}")


class GitHubAppIdentityError(GitHubAppError):
    pass


class GitHubHTTPError(GitHubAppError):
    def __init__(self, status: int, operation: str):
        self.status, self.operation = status, operation
        super().__init__(f"GitHub {operation} failed (HTTP {status})")


class WindowsDPAPI:
    """Current-user DPAPI protection; the encrypted file is portable only as ciphertext."""

    class _Blob(ctypes.Structure):
        _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]

    @staticmethod
    def _buffer(data: bytes):
        array = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
        return array, WindowsDPAPI._Blob(len(data), ctypes.cast(array, ctypes.POINTER(ctypes.c_ubyte)))

    @classmethod
    def protect(cls, data: bytes) -> bytes:
        if os.name != "nt":
            raise GitHubAppError("Windows DPAPI credential storage is unavailable on this host")
        crypt32, kernel32 = ctypes.windll.crypt32, ctypes.windll.kernel32
        source_buffer, source = cls._buffer(data)
        _ = source_buffer
        output = cls._Blob()
        if not crypt32.CryptProtectData(ctypes.byref(source), "VCCP GitHub App key", None, None,
                                        None, 1, ctypes.byref(output)):
            raise GitHubAppError("Windows DPAPI could not protect the App key")
        try:
            return ctypes.string_at(output.pbData, output.cbData)
        finally:
            kernel32.LocalFree(output.pbData)

    @classmethod
    def unprotect(cls, data: bytes) -> bytes:
        if os.name != "nt":
            raise GitHubAppError("Windows DPAPI credential storage is unavailable on this host")
        crypt32, kernel32 = ctypes.windll.crypt32, ctypes.windll.kernel32
        source_buffer, source = cls._buffer(data)
        _ = source_buffer
        output = cls._Blob()
        if not crypt32.CryptUnprotectData(ctypes.byref(source), None, None, None, None, 1,
                                          ctypes.byref(output)):
            raise GitHubAppError("Windows DPAPI could not read the protected App key")
        try:
            return ctypes.string_at(output.pbData, output.cbData)
        finally:
            kernel32.LocalFree(output.pbData)


class DPAPIFileCredentialSource:
    """Loads a PEM private key from a machine-local, current-user DPAPI file."""

    def __init__(self, path: str | Path, dpapi=WindowsDPAPI):
        self.path = Path(path).expanduser()
        self.dpapi = dpapi

    def load_private_key(self):
        try:
            encrypted = self.path.read_bytes()
            if not encrypted:
                raise GitHubAppError("protected GitHub App credential source is empty")
            pem = self.dpapi.unprotect(encrypted)
            if not pem.startswith(b"-----BEGIN ") or b"PRIVATE KEY-----" not in pem[:100]:
                raise GitHubAppError("protected GitHub App credential source is not a PEM private key")
            return serialization.load_pem_private_key(pem, password=None)
        except GitHubAppError:
            raise
        except (OSError, ValueError, TypeError) as error:
            raise GitHubAppError("protected GitHub App credential source is unreadable") from error

    @classmethod
    def protect_pem_file(cls, source: str | Path, destination: str | Path, dpapi=WindowsDPAPI):
        """Explicit human-run bootstrap operation. It never prints or retains plaintext."""
        source_path, destination_path = Path(source).expanduser(), Path(destination).expanduser()
        try:
            pem = source_path.read_bytes()
            serialization.load_pem_private_key(pem, password=None)
            protected = dpapi.protect(pem)
            destination_path.parent.mkdir(parents=True, exist_ok=True)
            with destination_path.open("xb") as handle:
                handle.write(protected)
            if os.name != "nt":
                destination_path.chmod(0o600)
        except FileExistsError as error:
            raise GitHubAppError("protected credential destination already exists") from error
        except GitHubAppError:
            raise
        except (OSError, ValueError, TypeError) as error:
            raise GitHubAppError("could not protect GitHub App private key") from error


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def create_app_jwt(app_id: str | int, private_key, now: int | None = None) -> str:
    issued = int(time.time()) if now is None else int(now)
    header = _b64url(json.dumps({"alg": "RS256", "typ": "JWT"}, separators=(",", ":")).encode())
    payload = _b64url(json.dumps({"iss": str(app_id), "iat": issued - 60,
                                 "exp": issued + 540}, separators=(",", ":")).encode())
    message = f"{header}.{payload}".encode("ascii")
    signature = private_key.sign(message, padding.PKCS1v15(), hashes.SHA256())
    return f"{message.decode('ascii')}.{_b64url(signature)}"


class UrllibTransport:
    """HTTP transport which discards remote error bodies and credential-bearing URLs."""

    def __init__(self, opener: Callable = urlopen):
        self.opener = opener

    def request(self, method: str, url: str, token: str, payload: dict | None = None):
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                   "X-GitHub-Api-Version": "2022-11-28"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = Request(url, data=body, headers=headers, method=method)
        try:
            with self.opener(request, timeout=30) as response:
                raw = response.read()
                return response.status, json.loads(raw.decode("utf-8")) if raw else {}
        except HTTPError as error:
            # Read no response body; GitHub error bodies are not suitable for logs.
            raise GitHubHTTPError(error.code, method) from None
        except (OSError, URLError, TimeoutError, json.JSONDecodeError):
            raise GitHubAppError(f"GitHub {method} request failed") from None


class GitHubAppCredentialProvider:
    """Validates App installation and mints repo-scoped short-lived tokens in memory."""

    def __init__(self, app_id: str | int, credential_source: DPAPIFileCredentialSource,
                 target_repository: str, expected_app_slug: str | None = None,
                 api_url: str = "https://api.github.com", transport=None,
                 required_permissions: dict[str, str] | None = None,
                 clock: Callable[[], float] = time.time):
        if not str(app_id).isdigit() or int(app_id) <= 0:
            raise ValueError("github_app.app_id must be a positive integer")
        if not _REPO.fullmatch(target_repository):
            raise ValueError("target repository must be OWNER/REPO")
        if api_url.rstrip("/") != "https://api.github.com" and not api_url.rstrip("/").endswith("/api/v3"):
            raise ValueError("GitHub Enterprise api_url must end in /api/v3")
        self.app_id, self.source = str(app_id), credential_source
        self.target_repository = target_repository.casefold()
        self.expected_app_slug = expected_app_slug.casefold() if expected_app_slug else None
        self.api_url = api_url.rstrip("/")
        self.transport = transport or UrllibTransport()
        self.required_permissions = dict(required_permissions or REQUIRED_PERMISSIONS)
        self.clock = clock
        self._private_key = None
        self._tokens: dict[str, tuple[str, float]] = {}

    def _repo(self, repo: str) -> tuple[str, str]:
        if not isinstance(repo, str) or not _REPO.fullmatch(repo) or repo.casefold() != self.target_repository:
            raise GitHubAppIdentityError("GitHub App provider is fenced to its configured target repository")
        owner, name = repo.split("/", 1)
        return owner, name

    def _key(self):
        if self._private_key is None:
            self._private_key = self.source.load_private_key()
        return self._private_key

    def _call(self, method: str, path: str, token: str, payload=None):
        status, response = self.transport.request(method, self.api_url + path, token, payload)
        return status, response

    def _installation(self, repo: str, *, fail_on_insufficient: bool = True):
        owner, name = self._repo(repo)
        jwt = create_app_jwt(self.app_id, self._key(), int(self.clock()))
        status, app = self._call("GET", "/app", jwt)
        if status != 200 or str(app.get("id")) != self.app_id or (
                self.expected_app_slug and str(app.get("slug", "")).casefold() != self.expected_app_slug):
            raise GitHubAppIdentityError("GitHub App identity did not match configured App")
        path = f"/repos/{owner}/{name}/installation"
        try:
            status, installation = self._call("GET", path, jwt)
        except GitHubHTTPError as error:
            if error.status == 404:
                raise GitHubAppNotInstalled(
                    f"GitHub App is not installed on {repo}; a human must install it"
                ) from None
            raise
        if status == 404:
            raise GitHubAppNotInstalled(f"GitHub App is not installed on {repo}; a human must install it")
        account = installation.get("account") or {}
        if str(account.get("login", "")).casefold() != owner.casefold():
            raise GitHubAppIdentityError("GitHub App installation owner does not match target repository")
        permissions = installation.get("permissions") or {}
        missing = {name: level for name, level in self.required_permissions.items()
                   if not self._permission_sufficient(permissions.get(name), level)}
        if missing and fail_on_insufficient:
            raise GitHubAppPermissionError(missing)
        installation_id = installation.get("id")
        if not isinstance(installation_id, int) or installation_id <= 0:
            raise GitHubAppError("GitHub App installation response was invalid")
        return owner, name, installation_id, missing

    @staticmethod
    def _permission_sufficient(granted, required):
        return granted == "write" if required == "write" else granted in {"read", "write"}

    def get_token(self, repo: str) -> str:
        self._repo(repo)
        cached = self._tokens.get(repo.casefold())
        now = float(self.clock())
        if cached and cached[1] - _APPROX_TOKEN_TTL.total_seconds() > now:
            return cached[0]
        owner, name, installation_id, missing = self._installation(repo)
        jwt = create_app_jwt(self.app_id, self._key(), int(self.clock()))
        _status, data = self._call(
            "POST", f"/app/installations/{installation_id}/access_tokens", jwt,
            {"repositories": [name], "permissions": self.required_permissions},
        )
        token = data.get("token")
        expires = data.get("expires_at")
        try:
            expiry = dt.datetime.fromisoformat(expires.replace("Z", "+00:00")).timestamp()
        except (AttributeError, TypeError, ValueError):
            raise GitHubAppError("GitHub installation token response was invalid") from None
        if not isinstance(token, str) or not token or expiry <= float(self.clock()) + 300:
            raise GitHubAppError("GitHub installation token expiry is insufficient")
        repositories = data.get("repositories")
        if not isinstance(repositories, list) or [str(item.get("full_name", "")).casefold()
                                                  for item in repositories if isinstance(item, dict)] != [repo.casefold()]:
            raise GitHubAppIdentityError("GitHub installation token repository scope did not match target")
        actual = data.get("permissions") or {}
        if actual != self.required_permissions:
            raise GitHubAppError("GitHub installation token permissions exceeded or missed the requested subset")
        missing = {name: level for name, level in self.required_permissions.items()
                   if not self._permission_sufficient(actual.get(name), level)}
        if missing:
            raise GitHubAppPermissionError(missing)
        self._tokens[repo.casefold()] = (token, expiry)
        return token

    def request(self, method: str, path: str, repo: str, payload=None):
        self._repo(repo)
        token = self.get_token(repo)
        return self._call(method, path, token, payload)

    def probe(self, repo: str) -> dict:
        owner, name, installation_id, missing = self._installation(repo, fail_on_insufficient=False)
        if missing:
            return {"installed": True, "permissions_sufficient": False,
                    "missing_permissions": missing, "installation_id": installation_id,
                    "repository": f"{owner}/{name}"}
        token = self.get_token(repo)
        return {"installed": True, "permissions_sufficient": True, "missing_permissions": [],
                "installation_id": installation_id, "repository": f"{owner}/{name}",
                "token_cached": repo.casefold() in self._tokens}


def repository_from_remote(remote: str, api_url: str = "https://api.github.com") -> str | None:
    """Extract OWNER/REPO from a GitHub HTTPS or SSH remote without accepting other hosts."""
    host = urlparse(api_url).hostname
    if host and host.casefold() == "api.github.com":
        host = "github.com"
    if not host or not isinstance(remote, str):
        return None
    candidate = remote.strip()
    if candidate.startswith("git@") or re.match(r"^[^/@:]+@[^/:]+:", candidate):
        match = re.match(r"^[^@]+@([^:]+):(.+)$", candidate)
        if not match or match.group(1).casefold() != host.casefold():
            return None
        path = match.group(2)
    else:
        parsed = urlparse(candidate)
        if parsed.hostname is None or parsed.hostname.casefold() != host.casefold():
            return None
        path = parsed.path.lstrip("/")
    path = path.removesuffix(".git").strip("/")
    return path if _REPO.fullmatch(path) else None


def protect_key_main(argv=None) -> int:
    import argparse
    parser = argparse.ArgumentParser(description="Protect a GitHub App PEM for the current Windows user.")
    parser.add_argument("source_pem")
    parser.add_argument("destination_dpapi_file")
    args = parser.parse_args(argv)
    try:
        DPAPIFileCredentialSource.protect_pem_file(args.source_pem, args.destination_dpapi_file)
    except GitHubAppError as error:
        print(str(error), file=sys.stderr)
        return 2
    print("GitHub App credential protected for the current user.")
    return 0


if __name__ == "__main__":
    raise SystemExit(protect_key_main())
