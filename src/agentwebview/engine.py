"""Engine protocol and normalized result types.

Engines are adapters: they receive an ``ExecutionContext`` built by the executor after the rules and the
limits have been checked and the call's cost has been reserved. A provider engine gets a callback that
decrypts the chosen key at the moment of the call, never the key store itself.
"""

from __future__ import annotations

import enum
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from agentwebview.contract import AnyTask
from agentwebview.errors import GatewayError
from agentwebview.policy import Policy


class NetworkClass(enum.StrEnum):
    LOCAL = "LOCAL"
    DIRECT_WEB = "DIRECT_WEB"
    COMMERCIAL_PROVIDER = "COMMERCIAL_PROVIDER"  # a connected provider API (bring your own key)


@dataclass(frozen=True)
class EngineDescriptor:
    id: str
    name: str
    operations: frozenset[str]
    network_class: NetworkClass
    coverage: frozenset[str]
    capabilities: frozenset[str]
    description: str
    replay_safe: bool = True
    price_denomination: str | None = None
    provider_id: str | None = None
    # Expected latency when nothing has been observed yet (used only for normalisation).
    default_latency_ms: int = 200
    # Minimum remaining deadline needed to attempt this engine.
    min_deadline_ms: int = 50
    reference_url: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "operations": sorted(self.operations),
            "network_class": self.network_class.value,
            "coverage": sorted(self.coverage),
            "capabilities": sorted(self.capabilities),
            "description": self.description,
            "price_denomination": self.price_denomination,
            "provider_id": self.provider_id,
            "reference_url": self.reference_url,
        }


@dataclass
class Evidence:
    source_id: str
    source_type: str
    version_digest: str
    excerpt: str
    title: str | None = None
    local_identifier: str | None = None
    original_url: str | None = None
    canonical_url: str | None = None
    document_id: str | None = None
    collection_id: str | None = None
    rank: float | None = None
    fetched_at: str | None = None
    indexed_at: str | None = None
    imported_at: str | None = None
    published_at: str | None = None
    extraction_method: str | None = None
    truncated: bool = False
    content_chars: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


class CancelToken:
    def __init__(self) -> None:
        self._event = threading.Event()
        self.reason: str | None = None

    def cancel(self, reason: str) -> None:
        self.reason = self.reason or reason
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def wait(self, seconds: float) -> bool:
        return self._event.wait(seconds)


@dataclass
class ExecutionContext:
    task: AnyTask
    task_id: str
    agent_id: str | None
    policy: Policy
    deadline_monotonic: float
    cancel: CancelToken
    reserved: dict[str, int]
    app: Any  # the Service: database, configuration, collections
    # Reserve more units before the next page (crawls); raises BUDGET_EXHAUSTED when a limit is reached.
    reserve_more: Callable[[dict[str, int]], Any] | None = None
    # Settle a reserve_more handle with measured usage.
    settle_more: Callable[[Any, dict[str, int]], None] | None = None
    # Provider engines: the key chosen by the router and a callback that decrypts it at dispatch time.
    provider_key_id: str | None = None
    credentials: Callable[[], str] | None = None

    def remaining_ms(self) -> int:
        return max(0, int((self.deadline_monotonic - time.monotonic()) * 1000))

    def max_excerpt(self) -> int:
        return min(self.task.limits.max_excerpt_chars, self.policy.max_excerpt_chars)

    def max_results(self) -> int:
        return min(self.task.limits.max_results, self.policy.max_results)


@dataclass
class EngineOutcome:
    """Normalized engine result. ``usage`` is the measured consumption to settle."""

    ok: bool
    evidence: list[Evidence] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)
    coverage: dict[str, Any] = field(default_factory=dict)
    error: GatewayError | None = None
    transport_error: bool = False
    rate_limited_until: float | None = None  # epoch seconds
    partial: bool = False
    detail: dict[str, Any] = field(default_factory=dict)
    content: str | None = None  # full extracted text for read_page (bounded)


class Engine(Protocol):
    descriptor: EngineDescriptor

    def check_requirements(self, task: AnyTask, policy: Policy) -> list[str]:
        """Reasons the engine cannot satisfy this task (empty = eligible)."""
        ...

    def estimate_resources(self, task: AnyTask, policy: Policy) -> dict[str, int]:
        """Upper bound of resources to reserve before dispatch."""
        ...

    def execute(self, ctx: ExecutionContext) -> EngineOutcome: ...
