"""Production-independent VCCP execution core."""

from .core import (
    MAX_AUTOMATED_REPAIRS,
    LaunchDisposition,
    LaunchResult,
    RepairCandidate,
    RuntimeConfig,
    RuntimeCore,
    WorkflowSnapshot,
    compute_repair_key,
)
from .lifecycle import LifecycleDriver

__all__ = [
    "MAX_AUTOMATED_REPAIRS",
    "LaunchDisposition",
    "LaunchResult",
    "RepairCandidate",
    "RuntimeConfig",
    "RuntimeCore",
    "WorkflowSnapshot",
    "compute_repair_key",
    "LifecycleDriver",
]
