"""Single-machine production runner with deterministic one-cycle mode."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import tempfile
import time

from .adapters import build_runtime
from .orchestrator import run_once


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


def _load(path):
    with open(path, encoding="utf-8") as stream:
        return json.load(stream)


def main(argv=None):
    parser = argparse.ArgumentParser(description="VCCP production runtime")
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--runtime-config", required=True)
    parser.add_argument("--repo", required=True, help="OWNER/REPO consumer identity")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--one-cycle", action="store_true")
    mode.add_argument("--continuous", action="store_true")
    parser.add_argument("--poll-interval", type=float, default=30.0)
    args = parser.parse_args(argv)
    manifest, local = _load(args.manifest), _load(args.runtime_config)
    lock = RuntimeLock(args.repo, local["database_path"])
    try:
        with lock:
            runtime = build_runtime(manifest, local)
            if args.continuous:
                run_continuous(runtime, args.repo, local["owner_id"], args.poll_interval)
            else:
                print(json.dumps(run_cycle(runtime, args.repo, local["owner_id"]), ensure_ascii=False))
    except RuntimeAlreadyRunning as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
