"""Exact-revision VCCP source checkouts for machine-local Consumers."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import shutil
import subprocess


_SHA = re.compile(r"^[0-9a-f]{40}$", re.IGNORECASE)


def _git(path: Path, *args: str, check=True):
    result = subprocess.run(["git", "-C", str(path), *args], text=True,
                            capture_output=True, check=False)
    if check and result.returncode != 0:
        raise ValueError("Git could not inspect or prepare the requested VCCP revision")
    return result


def _common_git_directory(path: Path) -> Path:
    result = _git(path, "rev-parse", "--path-format=absolute", "--git-common-dir")
    return Path(result.stdout.strip()).resolve()


def _verify_checkout(source: Path, checkout: Path, revision: str) -> Path:
    if checkout.is_symlink() or not checkout.is_dir():
        raise ValueError("existing revision checkout is not a real directory")
    root = Path(_git(checkout, "rev-parse", "--show-toplevel").stdout.strip()).resolve()
    if root != checkout.resolve() or _common_git_directory(root) != _common_git_directory(source):
        raise ValueError("existing revision checkout does not belong to the configured VCCP source")
    actual = _git(root, "rev-parse", "--verify", "HEAD").stdout.strip().lower()
    branch = _git(root, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    if actual != revision.lower() or branch != "HEAD":
        raise ValueError("existing revision checkout has the wrong VCCP revision")
    dirty = _git(root, "status", "--porcelain=v1", "--untracked-files=all").stdout
    if dirty.strip():
        raise ValueError("existing revision checkout is dirty")
    return root


def _recreate_checkout(source: Path, checkout: Path) -> None:
    """Discard only a registered exact-path worktree owned by this VCCP source."""
    if checkout.is_symlink() or not checkout.is_dir():
        raise ValueError("existing revision checkout is not a real directory")
    root = Path(_git(checkout, "rev-parse", "--show-toplevel").stdout.strip()).resolve()
    if root != checkout.resolve() or _common_git_directory(root) != _common_git_directory(source):
        raise ValueError("existing revision checkout does not belong to the configured VCCP source")
    removed = _git(source, "worktree", "remove", "--force", str(checkout), check=False)
    if removed.returncode != 0:
        raise ValueError("Git could not recreate the managed VCCP runtime checkout")
    if checkout.exists():
        shutil.rmtree(checkout)


def ensure_runtime_checkout(source: str | Path, runtimes_root: str | Path,
                            revision: str) -> Path:
    """Return a clean detached worktree at a full SHA, preserving other revisions."""
    if not isinstance(revision, str) or not _SHA.fullmatch(revision):
        raise ValueError("VCCP runtime revision must be a full 40-character commit SHA")
    source_path = Path(source).expanduser().resolve()
    if not source_path.is_dir():
        raise ValueError("configured VCCP source checkout is unavailable")
    if not _git(source_path, "rev-parse", "--is-inside-work-tree").stdout.strip() == "true":
        raise ValueError("configured VCCP source is not a Git checkout")
    root = Path(runtimes_root).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    checkout = root / revision.lower()
    if checkout.exists() or checkout.is_symlink():
        try:
            return _verify_checkout(source_path, checkout, revision)
        except ValueError as error:
            if "does not belong" in str(error) or "not a real directory" in str(error):
                raise
            _recreate_checkout(source_path, checkout)

    object_check = _git(source_path, "cat-file", "-e", f"{revision.lower()}^{{commit}}", check=False)
    if object_check.returncode != 0:
        remotes = _git(source_path, "remote", check=False).stdout.splitlines()
        if "origin" not in remotes:
            raise ValueError("requested VCCP revision is not present and source has no origin remote")
        fetched = _git(source_path, "fetch", "--no-tags", "origin", revision.lower(), check=False)
        if fetched.returncode != 0 or _git(source_path, "cat-file", "-e",
                                           f"{revision.lower()}^{{commit}}", check=False).returncode != 0:
            raise ValueError("requested VCCP revision is not present in the configured source or origin")

    added = _git(source_path, "worktree", "add", "--detach", str(checkout), revision.lower(), check=False)
    if added.returncode != 0:
        # Concurrent deploys can race at worktree creation. Adopt only after the same
        # complete path, source ownership, exact SHA and cleanliness checks pass.
        if checkout.exists():
            return _verify_checkout(source_path, checkout, revision)
        raise ValueError("Git could not create the exact-revision VCCP runtime checkout")
    return _verify_checkout(source_path, checkout, revision)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Prepare a clean VCCP runtime at one pinned commit.")
    parser.add_argument("--source", required=True)
    parser.add_argument("--runtimes-root", required=True)
    parser.add_argument("--revision", required=True)
    args = parser.parse_args(argv)
    try:
        path = ensure_runtime_checkout(args.source, args.runtimes_root, args.revision)
        print(json.dumps({"status": "ready", "revision": args.revision.lower(),
                          "path": str(path)}, sort_keys=True))
        return 0
    except ValueError as error:
        print(json.dumps({"status": "blocked", "detail": str(error)}, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
