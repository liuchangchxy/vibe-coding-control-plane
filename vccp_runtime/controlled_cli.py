"""Attempt-scoped, bounded GitHub App commands exposed to an Implementer."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import subprocess

from .adapters import GitHubAppWriter
from .core import _Store
from .github_app import (DPAPIFileCredentialSource, GitHubAppCredentialProvider,
                         GitHubAppError, POSIXFileCredentialSource)
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
    source_type = source.get("type")
    if source_type not in ("windows_dpapi_file", "posix_file"):
        raise GitHubAppError("runtime credential source type is unsupported")
    source_obj = (
        DPAPIFileCredentialSource(source["path"])
        if source_type == "windows_dpapi_file"
        else POSIXFileCredentialSource(source["path"])
    )
    return GitHubAppCredentialProvider(
        app["app_id"], source_obj, context["repo"],
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
    commands.add_parser("complete-no-change")
    return parser


def run(argv=None) -> int:
    try:
        args = _parser().parse_args(argv)
        context = _load_context(args.context)
        if "runtime_root" in context:
            expected_root = Path(context["runtime_root"]).resolve()
            actual_root = Path(__file__).resolve().parent.parent
            if actual_root != expected_root:
                raise GitHubAppError("controlled CLI module resolution does not match expected runtime")
        provider = _provider(context)
        repo = context["repo"]
        if args.command == "complete-no-change":
            if not context.get("allow_no_change") or not context.get("attempt_id"):
                raise GitHubAppError("no-change completion is not authorized for this attempt")
            if context.get("repo") != repo:
                raise GitHubAppError("attempt repository provenance is invalid")
            head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=context["workspace"],
                                  check=True, capture_output=True, text=True).stdout.strip()
            status = subprocess.run(["git", "status", "--porcelain=v1", "--untracked-files=all", "--", "."],
                                    cwd=context["workspace"], check=True, capture_output=True,
                                    text=True).stdout
            evidence = json.dumps({"head": head, "status": status}, sort_keys=True)
            baseline = context.get("workspace_baseline")
            if baseline and evidence != baseline:
                raise GitHubAppError("workspace differs from launch baseline")
            if not context.get("database_path"):
                raise GitHubAppError("runtime receipt store is unavailable")
            store = _Store(context["database_path"])
            attempt = store.attempt(context["attempt_id"])
            if (not attempt or attempt["repo"] != repo.casefold() or attempt["implementer_provider"] != context.get("provider")
                    or attempt["frozen_spec_digest"] != context.get("authorization_digest")):
                raise GitHubAppError("attempt provenance does not authorize completion")
            if store.has_controlled_push(context["attempt_id"]):
                raise GitHubAppError("no-change completion is unavailable after controlled push")
            if not store.record_completion(context["attempt_id"], attempt["implementer_provider"],
                                           attempt["execution_id"], evidence):
                raise GitHubAppError("attempt provenance does not authorize completion")
            return 0
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
            if args.operation == "issue" and args.issue_operation == "close":
                raise GitHubAppError("only Runtime may close a Frozen Issue")
            raise GitHubAppError("GitHub App operation is not allowed")
        if args.command == "git-push":
            if os.environ.get("VCCP_CONTROLLED_PUSH") != "1":
                raise GitHubAppError("push is available only through its attempt-scoped wrapper")
            if not context.get("attempt_id") or not context.get("provider") or not context.get("database_path"):
                raise GitHubAppError("attempt-scoped push audit is unavailable")
            store = _Store(context["database_path"])
            backend = GitHubAppPushBackend(provider, context["workspace"], repo,
                                           context["base_branch"])
            push_id = store.begin_controlled_push(context["attempt_id"], repo, context["provider"],
                                                  args.branch, args.expected_sha)
            if push_id is None:
                raise GitHubAppError("attempt provenance does not authorize controlled push")
            result = backend.push(repo, args.branch, args.expected_sha, args.branch, controlled=True)
            if (not isinstance(result, dict) or result.get("status") != "pushed"
                    or not store.complete_controlled_push(push_id, result.get("sha"))):
                raise GitHubAppError("controlled push result could not be durably recorded")
            return 0
        raise GitHubAppError("controlled operation is not allowed")
    except GitHubAppError as error:
        print(str(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(run())
