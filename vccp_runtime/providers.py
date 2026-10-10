"""Provider registry and deterministic, fail-closed launch routing.

RuntimeCore depends on one provider-neutral ``ImplementerPort``. This module owns the
ordered provider selection behind that port, so concrete adapters (AntiGravity today)
are registered here instead of being injected as the production singleton.
"""

from __future__ import annotations

import re
from typing import Iterable, Protocol

from .core import LaunchDisposition, LaunchRequest, LaunchResult


ANTIGRAVITY_PROVIDER = "antigravity"
CLAUDE_CODE_PROVIDER = "claude_code"
_PROVIDER_KEY = re.compile(r"^[a-z][a-z0-9_]*$")


class ProviderAdapter(Protocol):
    """Provider-owned behavior; only an adapter knows provider-specific launch details."""

    def launch(self, request: LaunchRequest) -> LaunchResult: ...

    def latest_activity(self, execution_id: str) -> float | None: ...


def normalize_provider_key(value: object) -> str:
    if not isinstance(value, str) or not _PROVIDER_KEY.fullmatch(value):
        raise ValueError("implementer provider key must be a lowercase identifier")
    return value


class ProviderRouter:
    """The one Implementer RuntimeCore sees: ordered launch plus provenance-routed observation.

    The first configured provider is primary; later providers are fallbacks in order. A
    provider is consulted only while every earlier provider proved that it definitely did
    not start work, so CONFIRMED and UNKNOWN both end provider selection immediately.
    """

    def __init__(self, providers: Iterable[tuple[str, ProviderAdapter]]):
        ordered: list[tuple[str, ProviderAdapter]] = []
        for key, adapter in providers:
            key = normalize_provider_key(key)
            if any(key == configured for configured, _ in ordered):
                raise ValueError(f"duplicate implementer provider key: {key}")
            if adapter is None:
                raise ValueError(f"implementer provider {key} has no adapter")
            ordered.append((key, adapter))
        if not ordered:
            raise ValueError("at least one implementer provider must be configured")
        self._ordered = tuple(ordered)
        self._by_key = dict(ordered)

    @property
    def primary(self) -> str:
        return self._ordered[0][0]

    def launch(self, request: LaunchRequest) -> LaunchResult:
        for key, adapter in self._ordered:
            try:
                result = adapter.launch(request)
            except Exception:
                # An adapter that raised cannot prove the external work did not start, so
                # the uncertainty belongs to that provider and must never reroute.
                return LaunchResult(LaunchDisposition.UNKNOWN, provider=key)
            if result.disposition == LaunchDisposition.CONFIRMED:
                return LaunchResult(LaunchDisposition.CONFIRMED, result.execution_id, key)
            if result.disposition == LaunchDisposition.UNKNOWN:
                return LaunchResult(LaunchDisposition.UNKNOWN, provider=key)
            # DEFINITELY_NOT_STARTED is the only outcome that may fall through.
        return LaunchResult(LaunchDisposition.DEFINITELY_NOT_STARTED)

    def latest_activity_for(self, provider: str | None, execution_id: str) -> float | None:
        adapter = self._by_key.get(provider) if isinstance(provider, str) else None
        if adapter is None or not execution_id:
            # Missing or unregistered provenance fails closed. Another provider is never
            # queried for an execution it does not own.
            return None
        return adapter.latest_activity(execution_id)
