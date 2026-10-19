"""Thin host deployment orchestration over readiness and native host primitives."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

from .activation import qualify_activation
from .host_deployment import (reconcile_macos_launch_agent, remove_macos_launch_agent,
                              reconcile_windows_sidecar, remove_windows_sidecar)
from .runtime_checkout import ensure_runtime_checkout


def deploy_host(*, repo: str, manifest: dict, local_config: dict,
                manifest_path: str, runtime_config_path: str, source_root: str,
                runtimes_root: str | None = None, platform: str | None = None,
                readiness=None, host_reconciler=None, runner=subprocess.run,
                host: str = "github.com") -> dict:
    revision = manifest.get("vccp", {}).get("revision")
    runtime_root = ensure_runtime_checkout(source_root,
        runtimes_root or str(Path.home() / ".vccp" / "runtimes"), revision)
    preflight = (readiness(runtime_root) if readiness else
                 qualify_activation(manifest, local_config, repo, host=host, check_runtime_lock=False))
    if not isinstance(preflight, dict) or preflight.get("status") != "ready":
        return {"status": "blocked", "revision": revision, "runtime_root": str(runtime_root),
                "reason": "machine readiness preflight did not pass", "readiness": preflight}
    def full_readiness(_runtime):
        return (readiness(runtime_root) if readiness else
                qualify_activation(manifest, local_config, repo, host=host, check_runtime_lock=True))
    selected_platform = platform or sys.platform
    if selected_platform == "darwin":
        dispatcher = local_config.get("dispatcher") or {}
        slug = repo.split("/")[-1].lower().replace("_", "-")
        label = dispatcher.get("label", f"com.vccp.{slug}-dispatcher")
        plist_path = dispatcher.get("plist_path", str(Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"))
        readiness_result = {}
        def readiness_after_stop():
            qualified = full_readiness(runtime_root)
            readiness_result["value"] = qualified
            return qualified
        result = (host_reconciler or reconcile_macos_launch_agent)(
            repo=repo, runtime_root=str(runtime_root), manifest_path=manifest_path,
            runtime_config_path=runtime_config_path, database_path=local_config["database_path"],
            python_executable=sys.executable, label=label, plist_path=plist_path,
            user_id=str(os.getuid()), readiness=readiness_after_stop, runner=runner)
        return {**result, "revision": revision, "runtime_root": str(runtime_root),
                "readiness": readiness_result.get("value")}
    if selected_platform == "win32":
        dispatcher = local_config.get("dispatcher") or {}
        slug = repo.split("/")[-1].lower().replace("_", "-")
        sidecar_path = dispatcher.get("sidecar_path") or str(
            Path.home() / ".gemini" / "config" / "sidecars" / f"{slug}-vccp" / "sidecar.json")
        readiness_result = {}
        def readiness_after_stop():
            qualified = full_readiness(runtime_root)
            readiness_result["value"] = qualified
            return qualified
        result = (host_reconciler or reconcile_windows_sidecar)(
            repo=repo, runtime_root=str(runtime_root), manifest_path=manifest_path,
            runtime_config_path=runtime_config_path, database_path=local_config["database_path"],
            python_executable=sys.executable, sidecar_path=sidecar_path,
            readiness=readiness_after_stop, runner=runner)
        return {**result, "revision": revision, "runtime_root": str(runtime_root),
                "readiness": readiness_result.get("value")}
    return {"status": "blocked", "revision": revision, "runtime_root": str(runtime_root),
            "reason": f"unsupported resident host platform: {selected_platform}"}


def remove_host(*, repo: str, local_config: dict, runtime_config_path: str,
                platform: str | None = None, runner=subprocess.run) -> dict:
    selected_platform = platform or sys.platform
    if selected_platform == "darwin":
        dispatcher = local_config.get("dispatcher") or {}
        slug = repo.split("/")[-1].lower().replace("_", "-")
        label = dispatcher.get("label", f"com.vccp.{slug}-dispatcher")
        plist_path = dispatcher.get("plist_path", str(Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"))
        try:
            return remove_macos_launch_agent(repo=repo, database_path=local_config["database_path"],
                runtime_config_path=runtime_config_path,
                label=label, plist_path=plist_path, user_id=str(os.getuid()), runner=runner)
        except KeyError:
            return {"status": "blocked", "reason": "runtime config lacks the Consumer database identity required to verify RuntimeLock"}
    if selected_platform == "win32":
        dispatcher = local_config.get("dispatcher") or {}
        slug = repo.split("/")[-1].lower().replace("_", "-")
        sidecar_path = dispatcher.get("sidecar_path") or str(
            Path.home() / ".gemini" / "config" / "sidecars" / f"{slug}-vccp" / "sidecar.json")
        try:
            return remove_windows_sidecar(repo=repo, database_path=local_config["database_path"],
                runtime_config_path=runtime_config_path,
                sidecar_path=sidecar_path, runner=runner)
        except KeyError:
            return {"status": "blocked", "reason": "runtime config lacks the Consumer database identity required to verify RuntimeLock"}
    return {"status": "blocked", "reason": f"unsupported resident host platform: {selected_platform}"}


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        operation = payload.pop("operation", "deploy")
        result = remove_host(**payload) if operation == "remove" else deploy_host(**payload)
    except (KeyError, TypeError, ValueError, OSError) as error:
        result = {"status": "blocked", "reason": str(error)}
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("status") == "running" else 2


if __name__ == "__main__":
    raise SystemExit(main())
