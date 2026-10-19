"""Provider engines (bring your own key).

A provider engine is an ordinary routing engine in the ``COMMERCIAL_PROVIDER`` network class. It never holds
a key: the executor passes the key chosen by the router and a callback that decrypts it at dispatch time.
Every call is metered in the provider's own unit (``ProviderSpec.unit``), reserved before dispatch from the
key's allowance and settled with what the provider reported (or the price-table estimate when it reports
nothing).

Error handling is uniform across providers:

* 401 / 403: ``PROVIDER_AUTH_FAILED``; the executor takes that key out of routing and marks it invalid.
* 402 (and provider-specific equivalents): ``PROVIDER_CREDITS_EXHAUSTED``; the executor writes off the key's
  local allowance for the period and the router moves on to the next compatible key.
* 429: ``RATE_LIMITED`` with a cooldown from ``Retry-After`` (header or body).
* 408 / 5xx / timeouts / connection errors: transport failures that feed the circuit breaker; provider units
  are charged conservatively at the estimate because the call may have been billed.
"""

from __future__ import annotations

import hashlib
import math
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from agentwebview.contract import AnyTask, ReadPageInput, ReadPageTask, SearchInput, SearchTask
from agentwebview.engine import EngineDescriptor, EngineOutcome, Evidence, ExecutionContext, NetworkClass
from agentwebview.errors import ErrorCode, GatewayError
from agentwebview.policy import Policy, host_allowed
from agentwebview.providers.http import (
    ProviderHTTP,
    ProviderRequestStopped,
    ProviderResponse,
    ProviderTransportError,
    error_message,
)
from agentwebview.urls import UrlError, canonicalize, host_of

MAX_CONTENT_CHARS = 200_000
CREDIT_EXHAUSTED_STATUSES = frozenset({402})


@dataclass(frozen=True)
class BalanceReading:
    """What a provider's usage endpoint reported, in the provider's unit."""

    remaining: int | None
    used: int | None
    allowance: int | None
    period_start: datetime | None = None
    period_end: datetime | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "remaining": self.remaining,
            "used": self.used,
            "allowance": self.allowance,
            "period_start": self.period_start.isoformat() if self.period_start else None,
            "period_end": self.period_end.isoformat() if self.period_end else None,
        }


@dataclass(frozen=True)
class ProviderSpec:
    provider_id: str
    name: str
    hosts: frozenset[str]
    unit: str  # limit denomination, provider-qualified
    unit_label: str
    pricing_url: str
    docs_url: str
    signup_url: str
    terms_url: str | None = None
    operations: tuple[str, ...] = ("search",)
    # Whether a key can be checked without spending credit (a free usage/balance endpoint exists).
    free_key_check: bool = False
    # The documented free tier as (amount in ``unit``, "monthly" | "daily" | "lifetime"); pre-fills the
    # allowance when a key is added. None = no free tier to suggest.
    free_allowance: tuple[int, str] | None = None
    free_tier_text: str = ""
    notes: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "name": self.name,
            "unit": self.unit,
            "unit_label": self.unit_label,
            "operations": list(self.operations),
            "api_hosts": sorted(self.hosts),
            "pricing_url": self.pricing_url,
            "docs_url": self.docs_url,
            "signup_url": self.signup_url,
            "terms_url": self.terms_url,
            "free_key_check": self.free_key_check,
            "free_allowance": None
            if self.free_allowance is None
            else {"amount": self.free_allowance[0], "period": self.free_allowance[1]},
            "free_tier_text": self.free_tier_text,
            "notes": self.notes,
        }


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def web_evidence(
    *,
    provider: str,
    url: str,
    title: str | None,
    text: str,
    max_chars: int,
    rank: float | None,
    published: str | None,
    method: str,
) -> Evidence | None:
    try:
        canon = canonicalize(url)
    except UrlError:
        return None
    body = " ".join((text or "").split())
    excerpt = body[:max_chars]
    return Evidence(
        source_id="web:" + hashlib.sha256(canon.encode()).hexdigest()[:24],
        source_type="web_index",
        version_digest=hashlib.sha256(f"{canon}\n{body}".encode()).hexdigest(),
        excerpt=excerpt,
        title=(title or None),
        original_url=url,
        canonical_url=canon,
        rank=rank,
        fetched_at=now_iso(),
        published_at=published or None,
        extraction_method=f"{provider}:{method}",
        truncated=len(body) > max_chars,
        content_chars=len(body),
    )


def search_input(ctx: ExecutionContext) -> SearchInput:
    assert isinstance(ctx.task, SearchTask)
    return ctx.task.input


def read_input(ctx: ExecutionContext) -> ReadPageInput:
    assert isinstance(ctx.task, ReadPageTask)
    return ctx.task.input


class ProviderEngine:
    """Base class: subclasses declare ``spec`` and descriptor fields and implement the provider calls."""

    spec: ProviderSpec
    engine_id: str
    operations: frozenset[str]
    coverage: frozenset[str]
    capabilities: frozenset[str]
    default_latency_ms: int = 1500

    def __init__(self, config: Any) -> None:
        self.config = config
        self.descriptor = EngineDescriptor(
            id=self.engine_id,
            name=self.spec.name,
            operations=self.operations,
            network_class=NetworkClass.COMMERCIAL_PROVIDER,
            coverage=self.coverage,
            capabilities=self.capabilities,
            description=self.describe(),
            replay_safe=True,
            price_denomination=self.spec.unit,
            provider_id=self.spec.provider_id,
            default_latency_ms=self.default_latency_ms,
            min_deadline_ms=1_000,
            reference_url=self.spec.pricing_url,
        )

    # ------------------------------------------------------------------ to implement

    def describe(self) -> str:
        raise NotImplementedError

    def estimate_units(self, task: AnyTask, policy: Policy) -> int:
        raise NotImplementedError

    def call(self, http: ProviderHTTP, key: str, ctx: ExecutionContext) -> EngineOutcome:
        raise NotImplementedError

    def check_key(self, http: ProviderHTTP, key: str) -> BalanceReading | None:
        """A free call proving the key works (and, when available, reading the balance). None = no free check."""
        return None

    def credits_exhausted(self, resp: ProviderResponse) -> bool:
        return resp.status in CREDIT_EXHAUSTED_STATUSES

    # ------------------------------------------------------------------ engine protocol

    def check_requirements(self, task: AnyTask, policy: Policy) -> list[str]:
        if isinstance(task, ReadPageTask):
            inp = task.input
            if inp.url is None:
                return ["needs a URL (stored documents are read by owned_corpus_search)"]
            try:
                host = host_of(canonicalize(inp.url))
            except UrlError:
                return ["invalid URL"]
            if not host_allowed(policy.direct_web, host):
                return ["destination host is not permitted by policy"]
        elif not isinstance(task, SearchTask):
            return [f"does not support operation '{task.operation}'"]
        return []

    def base_usage(self, task: AnyTask) -> dict[str, int]:
        if task.operation == "search":
            return {"task_calls": 1, "search_calls": 1}
        return {"task_calls": 1, "fetched_pages": 1}

    def estimate_resources(self, task: AnyTask, policy: Policy) -> dict[str, int]:
        return {**self.base_usage(task), self.spec.unit: max(1, self.estimate_units(task, policy))}

    def execute(self, ctx: ExecutionContext) -> EngineOutcome:
        if ctx.cancel.cancelled or ctx.remaining_ms() <= 0:
            code = ErrorCode.CANCELED if ctx.cancel.cancelled else ErrorCode.DEADLINE_EXCEEDED
            return EngineOutcome(ok=False, error=GatewayError(code, code.value), usage={})
        if ctx.credentials is None:
            return self._fail(
                ctx,
                GatewayError(ErrorCode.PROVIDER_AUTH_FAILED, f"no key is available for this {self.spec.name} account"),
                detail={"auth_failure": True},
            )
        key = ctx.credentials()
        try:
            with ProviderHTTP(
                self.config,
                self.spec.provider_id,
                self.spec.hosts,
                cancel=ctx.cancel,
                deadline_monotonic=ctx.deadline_monotonic,
            ) as http:
                return self.call(http, key, ctx)
        except ProviderRequestStopped as exc:
            # An interrupted dispatched call may have been billed. Settle the estimate and use a
            # terminal error code so the executor cannot replay it through another provider.
            return EngineOutcome(
                ok=False,
                usage=dict(ctx.reserved) if exc.dispatched else {},
                error=GatewayError(exc.code, f"{self.spec.name}: request stopped ({exc.code.value})"),
                detail={"provider": self.spec.provider_id, "interrupted": True, "dispatched": exc.dispatched},
            )
        except ProviderTransportError as exc:
            return EngineOutcome(
                ok=False,
                transport_error=True,
                usage=dict(ctx.reserved),  # the call may have been billed: charge the reservation
                error=GatewayError(
                    ErrorCode.ENGINE_FAILURE, f"{self.spec.name}: transport error ({exc})", retryable=True
                ),
                detail={"provider": self.spec.provider_id},
            )

    # ------------------------------------------------------------------ helpers for subclasses

    def timeout_s(self, ctx: ExecutionContext) -> float:
        return max(0.0, ctx.deadline_monotonic - time.monotonic())

    def max_excerpt(self, ctx: ExecutionContext) -> int:
        return ctx.max_excerpt()

    def max_results(self, ctx: ExecutionContext) -> int:
        return ctx.max_results()

    def usage(self, ctx: ExecutionContext, units: int | None) -> dict[str, int]:
        """Settlement usage: base units plus provider units (reported, else the reservation estimate)."""
        out = self.base_usage(ctx.task)
        out[self.spec.unit] = int(units) if units is not None else int(ctx.reserved.get(self.spec.unit, 0))
        return out

    def _fail(
        self,
        ctx: ExecutionContext,
        error: GatewayError,
        *,
        detail: dict[str, Any] | None = None,
        units: int = 0,
        transport: bool = False,
        rate_limited_until: float | None = None,
    ) -> EngineOutcome:
        return EngineOutcome(
            ok=False,
            error=error,
            usage=self.usage(ctx, units),
            transport_error=transport,
            rate_limited_until=rate_limited_until,
            detail={"provider": self.spec.provider_id, **(detail or {})},
        )

    def failure(self, ctx: ExecutionContext, resp: ProviderResponse) -> EngineOutcome:
        """Map a non-success provider response to a normalized failed outcome."""
        msg = error_message(resp)
        name = self.spec.name
        if resp.status in (401, 403):
            return self._fail(
                ctx,
                GatewayError(
                    ErrorCode.PROVIDER_AUTH_FAILED,
                    f"{name} rejected the stored key ({msg})",
                    remediation="replace the key on the Providers screen, then check it",
                ),
                detail={"auth_failure": True, "status": resp.status},
            )
        if self.credits_exhausted(resp):
            return self._fail(
                ctx,
                GatewayError(
                    ErrorCode.PROVIDER_CREDITS_EXHAUSTED,
                    f"{name} reports this key's credits are exhausted ({msg})",
                    remediation="the router moves on to another provider; the allowance comes back when it renews",
                ),
                detail={"credits_exhausted": True, "status": resp.status},
            )
        if resp.status == 429:
            wait = resp.retry_after_seconds()
            return self._fail(
                ctx,
                GatewayError(
                    ErrorCode.RATE_LIMITED,
                    f"{name} rate limit ({msg})",
                    retryable=True,
                    retry_after_ms=int(wait * 1000),
                ),
                rate_limited_until=time.time() + wait,
                detail={"status": 429},
            )
        if resp.status == 408 or resp.status >= 500:
            return self._fail(
                ctx,
                GatewayError(ErrorCode.ENGINE_FAILURE, f"{name} is unavailable ({msg})", retryable=True),
                transport=True,
                units=int(ctx.reserved.get(self.spec.unit, 0)),
                detail={"status": resp.status},
            )
        return self._fail(
            ctx,
            GatewayError(ErrorCode.ENGINE_FAILURE, f"{name} refused the request ({msg})"),
            detail={"status": resp.status},
        )


def usd_to_micro(value: Any) -> int | None:
    """Provider-reported USD amount to integer micro-USD, rounded up (conservative)."""
    try:
        return math.ceil(float(value) * 1_000_000 - 1e-9)
    except (TypeError, ValueError):
        return None


EngineFactory = Callable[[Any], ProviderEngine]
