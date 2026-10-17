"""Credential-safe GitHub App bridge for the consumer onboarding CLI."""
from __future__ import annotations

import json
from urllib.parse import quote
import sys

from .github_app import GitHubAppCredentialProvider, GitHubAppError, GitHubHTTPError, make_credential_source


def dispatch(request: dict, *, provider=None):
    action = request.get("action")
    repo = request.get("repo")
    if not isinstance(repo, str) or repo.count("/") != 1:
        raise ValueError("GitHub operation requires OWNER/REPO")
    owner, name = repo.split("/", 1)
    repo_path = f"/repos/{quote(owner, safe='')}/{quote(name, safe='')}"
    branch = quote(str(request.get("branch", "")), safe="")
    sha = quote(str(request.get("sha", "")), safe="")
    endpoints = {
        "getRepository": ("GET", repo_path, None),
        "getBranch": ("GET", f"{repo_path}/branches/{branch}", None),
        "getCommit": ("GET", f"{repo_path}/commits/{sha}", None),
        "listLabels": ("GET", f"{repo_path}/labels?per_page=100", None),
        "createLabel": ("POST", f"{repo_path}/labels", request.get("label")),
        "getBranchProtection": ("GET", f"{repo_path}/branches/{branch}/protection", None),
        "listRulesets": ("GET", f"{repo_path}/rulesets?includes_parents=true&per_page=100", None),
        "createBlob": ("POST", f"{repo_path}/git/blobs", request.get("blob")),
        "createTree": ("POST", f"{repo_path}/git/trees", request.get("tree")),
        "createCommit": ("POST", f"{repo_path}/git/commits", request.get("commit")),
        "createRef": ("POST", f"{repo_path}/git/refs", request.get("ref")),
        "getRef": ("GET", f"{repo_path}/git/ref/heads/{branch}", None),
        "listPullRequests": ("GET", f"{repo_path}/pulls?state=open&per_page=100", None),
        "createPullRequest": ("POST", f"{repo_path}/pulls", request.get("pull_request")),
    }
    if action not in endpoints:
        raise ValueError("GitHub operation is not available to the onboarding client")
    method, path, body = endpoints[action]
    if not provider:
        raise ValueError("GitHub App provider is required")
    try:
        status, payload = provider.request(method, path, repo, body)
    except GitHubHTTPError as error:
        if error.status == 404 and action in {"getBranch", "getBranchProtection", "getRef"}:
            return None
        if error.status == 403 and action in {"getBranchProtection", "listRulesets"}:
            return {"readable": False, "reason": "insufficient permission to read repository policy"}
        raise
    if status == 404 and action in {"getBranch", "getBranchProtection", "getRef"}:
        return None
    if status == 403 and action in {"getBranchProtection", "listRulesets"}:
        return {"readable": False, "reason": "insufficient permission to read repository policy"}
    if status < 200 or status >= 300:
        raise GitHubHTTPError(status, action)
    if action == "getBranchProtection":
        return {"readable": True, "value": payload}
    if action == "listRulesets":
        return {"readable": True, "value": payload}
    if action == "listLabels":
        return [{k: item.get(k) for k in ("name", "color", "description")} for item in payload]
    return payload


def main() -> int:
    try:
        request = json.load(sys.stdin)
        config = request["runtime_config"]
        app = config["github_app"]
        repo = request["repo"]
        source = make_credential_source(app["credential_source"])
        provider = GitHubAppCredentialProvider(app["app_id"], source, repo,
            expected_app_slug=app.get("expected_app_slug"), api_url=app.get("api_url", "https://api.github.com"))
        result = dispatch(request, provider=provider)
        print(json.dumps({"ok": True, "result": result}, ensure_ascii=False))
        return 0
    except (KeyError, TypeError, ValueError, GitHubAppError) as error:
        status = error.status if isinstance(error, GitHubHTTPError) else 0
        print(json.dumps({"ok": False, "status": status, "error": str(error),
                          "action": request.get("action") if isinstance(request, dict) else None}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
