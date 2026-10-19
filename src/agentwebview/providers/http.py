"""Bounded HTTPS requests to a provider's declared API hosts.

Only ``https://`` URLs on the provider's own hosts are allowed, redirects are never followed, the body is
capped, and a total deadline covers the whole call. Connections go through the same egress guard as page
reads, so a provider hostname that resolves to a private address is refused.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import httpx

from agentwebview.egress import EgressBlocked, EgressPolicy, GuardedTransport
from agentwebview.engine import CancelToken
from agentwebview.errors import ErrorCode, GatewayError

# Test deployments may answer provider calls from a fake transport (keyed by provider id).
TEST_TRANSPORTS: dict[str, httpx.BaseTransport] = {}


@dataclass
class ProviderResponse:
    status: int
    body: Any  # parsed JSON (dict/list) or None
    text: str
    headers: dict[str, str] = field(default_factory=dict)
    elapsed_ms: float = 0.0

    def retry_after_seconds(self, default: float = 30.0) -> float:
        raw = self.headers.get("retry-after")
        if raw:
            try:
                return max(1.0, min(float(raw), 3600.0))
            except ValueError:
                pass
        if isinstance(self.body, dict):
            for key in ("retryAfter", "retry_after"):
                value = self.body.get(key)
                if isinstance(value, int | float):
                    return max(1.0, min(float(value), 3600.0))
        return default


class ProviderTransportError(Exception):
    """The request may have reached the provider, or its answer was lost (timeout, connection, size)."""


class ProviderRequestStopped(Exception):
    def __init__(self, code: ErrorCode, *, dispatched: bool) -> None:
        self.code = code
        self.dispatched = dispatched
        super().__init__(code.value)


class ProviderHTTP:
    def __init__(
        self,
        config: Any,
        provider_id: str,
        hosts: frozenset[str],
        *,
        cancel: CancelToken | None = None,
        deadline_monotonic: float | None = None,
    ) -> None:
        self.config = config
        self.provider_id = provider_id
        self.hosts = hosts
        self.cancel = cancel
        self.deadline_monotonic = deadline_monotonic

    def __enter__(self) -> ProviderHTTP:
        return self

    def __exit__(self, *exc: Any) -> None:
        pass

    def _check_stopped(self, *, dispatched: bool) -> None:
        if self.cancel is not None and self.cancel.cancelled:
            raise ProviderRequestStopped(ErrorCode.CANCELED, dispatched=dispatched)
        if self.deadline_monotonic is not None and time.monotonic() >= self.deadline_monotonic:
            raise ProviderRequestStopped(ErrorCode.DEADLINE_EXCEEDED, dispatched=dispatched)

    def _transport(self) -> httpx.BaseTransport:
        if getattr(self.config, "test_mode", False) and self.provider_id in TEST_TRANSPORTS:
            return TEST_TRANSPORTS[self.provider_id]
        return GuardedTransport(EgressPolicy.from_config(self.config))

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        json_body: Any = None,
        params: dict[str, Any] | None = None,
        timeout_s: float | None = None,
    ) -> ProviderResponse:
        self._check_stopped(dispatched=False)
        parts = urlsplit(url)
        if parts.scheme != "https" or (parts.hostname or "").lower() not in self.hosts:
            raise GatewayError(
                ErrorCode.NETWORK_BLOCKED,
                f"{self.provider_id}: {parts.hostname} is not one of this provider's declared API hosts",
            )
        limit_s = self.config.provider_timeout_ms / 1000
        timeout = min(limit_s, timeout_s if timeout_s is not None else limit_s)
        if self.deadline_monotonic is not None:
            timeout = min(timeout, self.deadline_monotonic - time.monotonic())
        if timeout <= 0:
            raise ProviderRequestStopped(ErrorCode.DEADLINE_EXCEEDED, dispatched=False)
        cap = self.config.provider_max_response_bytes
        t0 = time.monotonic()
        try:
            with httpx.Client(transport=self._transport(), trust_env=False, follow_redirects=False) as client:
                with client.stream(
                    method,
                    url,
                    headers={"Accept": "application/json", **headers},
                    json=json_body,
                    params=params,
                    timeout=httpx.Timeout(timeout, connect=min(10.0, timeout)),
                ) as resp:
                    chunks: list[bytes] = []
                    size = 0
                    for chunk in resp.iter_bytes():
                        self._check_stopped(dispatched=True)
                        if time.monotonic() - t0 > timeout:
                            raise ProviderTransportError("total request deadline exceeded")
                        size += len(chunk)
                        if size > cap:
                            raise ProviderTransportError(f"response larger than {cap} bytes")
                        chunks.append(chunk)
                    raw = b"".join(chunks)
                    status = resp.status_code
                    hdrs = {k.lower(): v for k, v in resp.headers.items()}
        except EgressBlocked as exc:
            raise GatewayError(ErrorCode.NETWORK_BLOCKED, f"{self.provider_id}: {exc}") from exc
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            self._check_stopped(dispatched=True)
            raise ProviderTransportError(type(exc).__name__) from exc
        self._check_stopped(dispatched=True)
        text = raw.decode("utf-8", errors="replace")
        body: Any = None
        if text.strip():
            try:
                body = json.loads(text)
            except ValueError:
                body = None
        return ProviderResponse(status, body, text[:4000], hdrs, (time.monotonic() - t0) * 1000)


def error_message(resp: ProviderResponse) -> str:
    """A short provider error string with no secrets (bodies never echo keys; still bounded)."""
    body = resp.body
    if isinstance(body, dict):
        for key in ("error", "message", "readableMessage", "detail", "tag"):
            value = body.get(key)
            if isinstance(value, dict):
                value = value.get("error") or value.get("message")
            if isinstance(value, str) and value.strip():
                return value.strip()[:300]
    return f"HTTP {resp.status}"
