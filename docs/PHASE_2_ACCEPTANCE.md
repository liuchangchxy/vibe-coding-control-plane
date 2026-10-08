# Phase 2 Acceptance Evidence

| Phase | Authoritative implementation and tests |
| --- | --- |
| P2-A — Contract alignment | `docs/ONBOARDING.md`, `schemas/control-plane.schema.json`, `tests/onboarding/onboarding.test.mjs` |
| P2-B — Generic core | `vccp_runtime/core.py`, `tests/python/test_runtime_core.py` |
| P2-C — Production adapters | `vccp_runtime/adapters.py`, `tests/python/test_production_adapters.py` |
| P2-D1 — Recovery and safety | `vccp_runtime/core.py`, `tests/python/test_recovery.py` |
| P2-D2 — CI / Review / Merge lifecycle | `vccp_runtime/lifecycle.py`, `tests/python/test_lifecycle.py` |
| P2-E — Final integration acceptance | `vccp_runtime/orchestrator.py`, `tests/python/test_final_integration.py` |

The Python suite is discovered by the repository CI workflow; the final integration
test drives `build_runtime()` with two consumer manifests, a shared temporary SQLite
database, and deterministic fake external transports.
