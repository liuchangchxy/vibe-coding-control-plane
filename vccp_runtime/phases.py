"""Shared attempt phase facts used by host lifecycle operations."""

ATTEMPT_TERMINAL_PHASES = frozenset({
    "CLAIM_FAILED",
    "LAUNCH_NOT_STARTED",
    "RETRY_ELIGIBLE",
    "BUDGET_EXHAUSTED",
    "MERGED_SUCCESS",
    "SUCCESS_NO_OP",
    "STOPPED_CANCELLED",
    "TERMINAL_UNRESOLVED",
    "COMPLETED",
    "FAILED",
})


def is_terminal_attempt_phase(phase: str, closeout_pending: bool = False) -> bool:
    return phase in ATTEMPT_TERMINAL_PHASES and not closeout_pending
