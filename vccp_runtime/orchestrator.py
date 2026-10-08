"""Thin one-shot sequencing for the existing VCCP runtime components."""
from __future__ import annotations


def run_once(runtime, repo: str, owner_id: str, issue_number: int | None = None,
             now: float | None = None) -> dict:
    """Optionally dispatch one named Issue, then advance and reconcile once.

    Discovery remains limited to the existing active-work recovery path. In
    particular, omitting ``issue_number`` never schedules agent-ready work.
    """
    dispatch = (runtime.core.dispatch_initial(repo, issue_number, owner_id)
                if issue_number is not None else {"status": "not_requested"})
    lifecycle = runtime.lifecycle.advance_once(repo, owner_id, now)
    # Lifecycle checks native merge first, so a linked Issue auto-closed by
    # GitHub is recorded as success before D1 observes closed-Issue fencing.
    recovery = runtime.core.reconcile_once(repo, owner_id, now)
    return {"dispatch": dispatch, "recovery": recovery, "lifecycle": lifecycle}
