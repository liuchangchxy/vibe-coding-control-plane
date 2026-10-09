"""Attempt-scoped, bounded GitHub App commands exposed to an Implementer."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

from .adapters import GitHubAppWriter
from .github_app import (DPAPIFileCredentialSource, GitHubAppCredentialProvider,
                         GitHubAppError)
from .github_push import GitHubAppPushBackend


def _load_context(path: str) -> dict:
    try:
        context = json.loads(Path(path).read_text(encoding="utf-8"))
        if (not isinstance(context, dict) or not isinstance(context.get("github_app"), dict)
                or not isinstance(context.get("repo"), str)
                or not isinstance(context.get("workspace"), str)):
            raise ValueError
        return context
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        raise GitHubAppError("attempt-scoped runtime context is unavailable") from None


def _provider(context):
    app = context["github_app"]
    source = app.get("credential_source") or {}
    if source.get("type") != "windows_dpapi_file":
        raise GitHubAppError("runtime credential source type is unsupported")
    return GitHubAppCredentialProvider(
        app["app_id"], DPAPIFileCredentialSource(source["path"]), context["repo"],
        app.get("expected_app_slug"), app.get("api_url", "https://api.github.com"),
    )


def _parser():
    parser = argparse.ArgumentParser(prog="vccp-controlled")
    parser.add_argument("--context", required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    app = commands.add_parser("app-gh")
    operations = app.add_subparsers(dest="operation", required=True)
    issue = operations.add_parser("issue")
    issue_ops = issue.add_subparsers(dest="issue_operation", required=True)
    edit = issue_ops.add_parser("edit")
    edit.add_argument("issue_number", type=int)
    edit.add_argument("--remove-label", action="append", default=[])
    edit.add_argument("--add-label", action="append", default=[])
    pr = operations.add_parser("pr")
    pr_ops = pr.add_subparsers(dest="pr_operation", required=True)
    create = pr_ops.add_parser("create")
    create.add_argument("--title", required=True)
    create.add_argument("--body", required=True)
    create.add_argument("--head", required=True)
    create.add_argument("--base", required=True)
    create.add_argument("--draft", action="store_true")
    push = commands.add_parser("git-push")
    push.add_argument("--branch", required=True)
    push.add_argument("--expected-sha", required=True)
    return parser


def run(argv=None) -> int:
    try:
        args = _parser().parse_args(argv)
        context = _load_context(args.context)
        provider = _provider(context)
        repo = context["repo"]
        if args.command == "app-gh":
            labels = context.get("coordination_labels") or []
            writer = GitHubAppWriter(credential_provider=provider, target_repository=repo,
                                     allowed_labels=labels)
            if args.operation == "issue" and args.issue_operation == "edit":
                if args.issue_number != context["issue_number"]:
                    raise GitHubAppError("writer is fenced to the active Frozen Issue")
                if not writer.replace_labels(repo, args.issue_number, args.remove_label, args.add_label):
                    raise GitHubAppError("controlled label operation was not verified")
                return 0
            if args.operation == "pr" and args.pr_operation == "create":
                if args.base != context["base_branch"]:
                    raise GitHubAppError("pull request base is outside consumer policy")
                writer.create_pull_request(repo, context["issue_number"], args.title, args.body,
                                            args.head, args.base, args.draft)
                return 0
            raise GitHubAppError("GitHub App operation is not allowed")
        if args.command == "git-push":
            if os.environ.get("VCCP_CONTROLLED_PUSH") != "1":
                raise GitHubAppError("push is available only through its attempt-scoped wrapper")
            backend = GitHubAppPushBackend(provider, context["workspace"], repo,
                                           context["base_branch"])
            backend.push(repo, args.branch, args.expected_sha, args.branch, controlled=True)
            return 0
        raise GitHubAppError("controlled operation is not allowed")
    except GitHubAppError as error:
        print(str(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(run())
