"""Production cycle orchestration for the Python-owned VCCP runtime."""
from __future__ import annotations


def run_once(runtime, repo: str, owner_id: str, issue_number: int | None = None,
             now: float | None = None) -> dict:
    """Run recovery, active lifecycle, and ready discovery in one bounded cycle.

    ``issue_number`` remains a diagnostic override. Production discovery does not
    require it and always rechecks eligibility through ``dispatch_initial``.
    """
    # Native merge is observed before Issue auto-closure is treated as cancellation.
    # Unwritten terminal summaries from an earlier cycle ride this same cycle, so a failed
    # comment write is retried without a daemon, timer, or extra queue.
    terminal_notices = (runtime.core.retry_pending_terminal_notices(repo, now)
                        if hasattr(runtime.core, "retry_pending_terminal_notices")
                        else {"status": "not_applicable", "items": []})
    no_op = (runtime.core.reconcile_no_change(repo, now)
             if hasattr(runtime.core, "reconcile_no_change") else {"status": "not_applicable", "items": []})
    lifecycle = runtime.lifecycle.advance_once(repo, owner_id, now)
    recovery = runtime.core.reconcile_once(repo, owner_id, now)
    progress = runtime.core.observe_implementer_progress(repo, now)
    try:
        candidates = ([issue_number] if issue_number is not None else
                      runtime.workflow.discover_ready(repo))
    except Exception as error:
        candidates = []
        ready_discovery = {"status": "discovery_unavailable", "detail": str(error)}
    else:
        ready_discovery = {"status": "discovered", "count": len(candidates)}
    dispatches = [runtime.core.dispatch_initial(repo, issue, owner_id) for issue in candidates]
    # Bind a newly-created PR / observe a newly advanced lifecycle in this cycle.
    lifecycle_after_dispatch = runtime.lifecycle.advance_once(repo, owner_id, now)
    diagnostic_dispatch = (dispatches[0] if issue_number is not None and dispatches
                           else {"status": "not_requested"})
    return {"dispatch": diagnostic_dispatch, "no_op": no_op, "recovery": recovery, "progress": progress,
            "terminal_notices": terminal_notices,
            "ready_discovery": ready_discovery, "lifecycle": lifecycle,
            "dispatches": dispatches, "lifecycle_after_dispatch": lifecycle_after_dispatch}
