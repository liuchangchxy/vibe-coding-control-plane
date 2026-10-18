"""Single-machine production runner with deterministic one-cycle mode."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import time

from .adapters import _workspace_repository, build_runtime
from .orchestrator import run_once


_REPORTED_CYCLE_STATUSES = frozenset({"needs-human", "infra-blocked", "implementer_stalled"})
_TOKEN_PATTERN = re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b")
_PRIVATE_KEY_PATTERN = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----", re.DOTALL
)


class RuntimeAlreadyRunning(RuntimeError):
    pass


class RuntimeLock:
    """Process exclusion keyed by consumer and local SQLite runtime identity."""
    def __init__(self, repo: str, database_path: str):
        identity = f"{repo.casefold()}\0{Path(database_path).expanduser().resolve()}".casefold()
        digest = hashlib.sha256(identity.encode()).hexdigest()[:24]
        self.path = Path(tempfile.gettempdir()) / f"vccp-runtime-{digest}.lock"
        self.file = None

    def acquire(self):
        self.file = self.path.open("a+b")
        self.file.seek(0, os.SEEK_END)
        if self.file.tell() == 0:
            self.file.write(b"\0")
            self.file.flush()
        self.file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError):
            self.file.close()
            self.file = None
            raise RuntimeAlreadyRunning(f"runtime already owns {self.path.name}") from None
        return self

    def release(self):
        if self.file is None:
            return
        if os.name == "nt":
            import msvcrt
            self.file.seek(0)
            msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(self.file.fileno(), fcntl.LOCK_UN)
        self.file.close()
        self.file = None

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *_):
        self.release()


def run_cycle(runtime, repo: str, owner_id: str, issue_number: int | None = None,
              now: float | None = None):
    return run_once(runtime, repo, owner_id, issue_number, now)


def run_continuous(runtime, repo: str, owner_id: str, poll_interval: float = 30.0,
                   sleep=time.sleep, stop=None, issue_number: int | None = None,
                   on_cycle=None):
    """Run cycles until the injected stop predicate or process termination."""
    if poll_interval <= 0:
        raise ValueError("poll_interval must be positive")
    stop = stop or (lambda: False)
    while not stop():
        result = run_cycle(runtime, repo, owner_id, issue_number)
        if on_cycle is not None:
            on_cycle(result)
        if not stop():
            sleep(poll_interval)


def _runtime_source_revision():
    """Read the exact commit of the VCCP checkout providing this module."""
    source_root = Path(__file__).resolve().parent.parent
    result = subprocess.run(
        ["git", "rev-parse", "--verify", "HEAD"], cwd=source_root,
        check=True, capture_output=True, text=True,
    )
    status = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"], cwd=source_root,
        check=True, capture_output=True, text=True,
    )
    if status.stdout.strip():
        raise ValueError("running VCCP checkout has uncommitted changes")
    return result.stdout.strip().lower()


def _validate_runtime_revision(manifest):
    vccp = manifest.get("vccp") if isinstance(manifest, dict) else None
    pin = vccp.get("revision") if isinstance(vccp, dict) else None
    if not isinstance(pin, str) or not re.fullmatch(r"[0-9a-fA-F]{40}", pin):
        raise ValueError("consumer manifest must pin a full 40-character VCCP revision")
    try:
        current = _runtime_source_revision()
    except (OSError, subprocess.CalledProcessError) as error:
        raise ValueError("cannot determine the running VCCP checkout revision") from error
    if current != pin.lower():
        raise ValueError(
            f"running VCCP checkout revision {current} does not match consumer manifest pin {pin.lower()}"
        )


def _safe_detail(value):
    detail = str(value)
    detail = _PRIVATE_KEY_PATTERN.sub("[REDACTED]", detail)
    detail = _TOKEN_PATTERN.sub("[REDACTED]", detail)
    return detail[:1000]


def _cycle_failures(result):
    failures = []

    def visit(value, path):
        if isinstance(value, dict):
            status = value.get("status")
            if isinstance(status, str) and (
                "unavailable" in status or "error" in status or "failed" in status
                or "blocked" in status or "stalled" in status
                or status in _REPORTED_CYCLE_STATUSES
            ):
                failure = {"component": path, "status": status}
                if "detail" in value:
                    failure["detail"] = _safe_detail(value["detail"])
                if isinstance(value.get("issue"), int):
                    failure["issue"] = value["issue"]
                failures.append(failure)
            for key, child in value.items():
                if key not in {"status", "detail"}:
                    visit(child, f"{path}.{key}" if path else key)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                visit(child, f"{path}[{index}]")

    visit(result, "")
    return failures


def _report_cycle(result, repo: str, stream=None):
    """Emit only actionable machine-readable cycle failures; ordinary idle stays quiet."""
    stream = stream or sys.stderr
    for failure in _cycle_failures(result):
        event = ("ready_discovery_unavailable"
                 if failure["component"] == "ready_discovery"
                 and failure["status"] == "discovery_unavailable"
                 else "runtime_cycle_issue")
        record = {"event": event, "repo": repo, **failure}
        stream.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def _load(path):
    with open(path, encoding="utf-8") as stream:
        text = stream.read()
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        import yaml
        return yaml.safe_load(text)


def main(argv=None):
    parser = argparse.ArgumentParser(description="VCCP production runtime")
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--runtime-config", required=True)
    parser.add_argument("--repo", required=True, help="OWNER/REPO consumer identity")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--one-cycle", action="store_true")
    mode.add_argument("--continuous", action="store_true")
    mode.add_argument("--retry-reviewer", action="store_true",
                      help="one manually authorized retry of a failed exact-head Claude Reviewer")
    parser.add_argument("--issue", type=int, help="Frozen Issue number for --retry-reviewer")
    parser.add_argument("--pr", type=int, help="pull request number for --retry-reviewer")
    parser.add_argument("--head-sha", help="exact full PR head SHA for --retry-reviewer")
    parser.add_argument("--authorize-reviewer-retry-for", metavar="SHA",
                        help="explicitly authorize retry for this exact full SHA")
    parser.add_argument("--poll-interval", type=float, default=30.0)
    args = parser.parse_args(argv)
    retry_values = (args.issue, args.pr, args.head_sha, args.authorize_reviewer_retry_for)
    if args.retry_reviewer:
        if any(value is None for value in retry_values):
            parser.error("--retry-reviewer requires --issue, --pr, --head-sha, and "
                         "--authorize-reviewer-retry-for")
        if (not re.fullmatch(r"[0-9a-fA-F]{40}", args.head_sha)
                or args.authorize_reviewer_retry_for.casefold() != args.head_sha.casefold()):
            parser.error("reviewer retry authorization must repeat the exact full --head-sha")
        if args.issue < 1 or args.pr < 1:
            parser.error("--issue and --pr must be positive integers")
    elif any(value is not None for value in retry_values):
        parser.error("reviewer retry arguments require --retry-reviewer")
    manifest, local = _load(args.manifest), _load(args.runtime_config)
    try:
        _validate_runtime_revision(manifest)
    except ValueError as error:
        parser.error(str(error))
    target_repo = local.get("target_repository")
    enrollment = local.get("enrollment")
    if not isinstance(target_repo, str) or target_repo.casefold() != args.repo.casefold():
        parser.error("--repo must match machine-local target_repository")
    authorized_repo = enrollment.get("authorized_repository") if isinstance(enrollment, dict) else None
    if not isinstance(authorized_repo, str) or authorized_repo.casefold() != args.repo.casefold():
        parser.error("human enrollment authorization does not match --repo")
    if not isinstance(local.get("workspace"), str):
        parser.error("machine-local workspace is required")
    app_config = local.get("github_app")
    api_url = app_config.get("api_url", "https://api.github.com") if isinstance(app_config, dict) else "https://api.github.com"
    if _workspace_repository(local["workspace"], api_url).casefold() != args.repo.casefold():
        parser.error("workspace origin does not match --repo")
    lock = RuntimeLock(args.repo, local["database_path"])
    try:
        with lock:
            runtime = build_runtime(manifest, local)
            if args.retry_reviewer:
                if runtime.local_reviewer is None:
                    parser.error("configured runtime has no Local Reviewer")
                result = runtime.local_reviewer.retry_failed(
                    args.repo, args.issue, args.pr, args.head_sha,
                    authorization_head=args.authorize_reviewer_retry_for)
                print(json.dumps(result, ensure_ascii=False, sort_keys=True))
                if result.get("status") == "reviewer_running":
                    return 0
                if result.get("status") in {"reviewer_retry_rejected", "reviewer_retry_claim_lost"}:
                    return 2
                return 1
            if args.continuous:
                run_continuous(runtime, args.repo, local["owner_id"], args.poll_interval,
                               on_cycle=lambda result: _report_cycle(result, args.repo))
            else:
                print(json.dumps(run_cycle(runtime, args.repo, local["owner_id"]), ensure_ascii=False))
    except RuntimeAlreadyRunning as error:
        parser.error(str(error))


if __name__ == "__main__":
    raise SystemExit(main())
