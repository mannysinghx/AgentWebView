"""Bounded HTTP fetching through the guarded egress transport.

* connect / read / overall deadlines, cooperative cancellation
* manual redirects (up to the policy maximum) with scope and connection checks on every hop
* robots.txt per origin (4xx: allow, 5xx or unreachable: disallow, per RFC 9309)
* streamed body with a byte cap, gzip/deflate decoded incrementally with a ratio cap, a MIME allow-list
  and binary sniffing
* truncation is reported, never hidden
"""

from __future__ import annotations

import codecs
import re
import time
import urllib.robotparser
import zlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx

from agentwebview import __version__
from agentwebview.egress import EgressBlocked, EgressPolicy, GuardedTransport
from agentwebview.errors import ErrorCode, GatewayError
from agentwebview.policy import DirectWebPolicy, host_allowed
from agentwebview.urls import UrlError, canonicalize, host_of

ROBOTS_MAX_BYTES = 512 * 1024
UA_TOKEN = "AgentWebView"
USER_AGENT = f"AgentWebView/{__version__} (+https://github.com/mannysinghx/AgentWebView; open-source agent gateway)"
_CHARSET_META = re.compile(rb"""<meta[^>]+charset\s*=\s*["']?([A-Za-z0-9_.:-]{1,40})""", re.I)


class FetchFailure(Exception):
    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        transport: bool = False,
        blocked: bool = False,
        retry_after: float | None = None,
        status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.transport = transport
        self.blocked = blocked
        self.retry_after = retry_after
        self.status = status

    def as_gateway_error(self) -> GatewayError:
        return GatewayError(
            self.code,
            self.message,
            retryable=self.transport or self.retry_after is not None,
            retry_after_ms=int(self.retry_after * 1000) if self.retry_after else None,
        )


@dataclass
class FetchResult:
    requested_url: str
    final_url: str
    status: int
    content_type: str
    charset: str
    text: str
    wire_bytes: int
    body_bytes: int
    truncated: bool
    fetched_at: datetime
    redirects: list[dict[str, Any]] = field(default_factory=list)
    peer_address: str | None = None
    headers: dict[str, str] = field(default_factory=dict)


@dataclass
class FetchStats:
    requests: int = 0
    wire_bytes: int = 0


class Deadline:
    def __init__(self, seconds: float, cancel: Any = None) -> None:
        self.end = time.monotonic() + seconds
        self.cancel = cancel

    def remaining(self) -> float:
        return max(0.0, self.end - time.monotonic())

    def check(self) -> None:
        if self.cancel is not None and getattr(self.cancel, "cancelled", False):
            raise FetchFailure(ErrorCode.CANCELED, "fetch canceled")
        if self.remaining() <= 0:
            raise FetchFailure(ErrorCode.DEADLINE_EXCEEDED, "fetch deadline reached", transport=True)


class Fetcher:
    def __init__(
        self,
        policy: DirectWebPolicy,
        egress: EgressPolicy,
        *,
        extra_allowed_hosts: tuple[str, ...] | None = None,
    ) -> None:
        self.policy = policy
        self.transport = GuardedTransport(egress)
        self.client = httpx.Client(
            transport=self.transport,
            follow_redirects=False,
            trust_env=False,
            headers={
                "User-Agent": USER_AGENT,
                "Accept-Encoding": "gzip, deflate",
                "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,text/markdown;q=0.9,*/*;q=0.1",
            },
        )
        self.scope = set(extra_allowed_hosts) if extra_allowed_hosts is not None else None
        self.stats = FetchStats()
        self._robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}
        self.robots_delays: dict[str, float] = {}

    def close(self) -> None:
        self.client.close()

    def __enter__(self) -> Fetcher:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------ scope

    def check_scope(self, url: str) -> str:
        try:
            canon = canonicalize(url)
        except UrlError as exc:
            raise FetchFailure(ErrorCode.NETWORK_BLOCKED, f"invalid destination: {exc}", blocked=True) from exc
        host = host_of(canon)
        if not host_allowed(self.policy, host):
            raise FetchFailure(ErrorCode.NETWORK_BLOCKED, f"host {host} is not permitted by policy", blocked=True)
        if self.scope is not None and host not in self.scope:
            raise FetchFailure(ErrorCode.NETWORK_BLOCKED, f"host {host} is outside this crawl's scope", blocked=True)
        return canon

    # ------------------------------------------------------------------ robots

    def robots_allows(self, url: str, deadline: Deadline) -> bool:
        if not self.policy.respect_robots:
            return True
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self._robots:
            self._robots[origin] = self._load_robots(origin, deadline)
        parser = self._robots[origin]
        if parser is None:
            return False
        delay = parser.crawl_delay(UA_TOKEN)
        if delay:
            self.robots_delays[parts.netloc] = float(delay)
        return parser.can_fetch(UA_TOKEN, url)

    def _load_robots(self, origin: str, deadline: Deadline) -> urllib.robotparser.RobotFileParser | None:
        rp = urllib.robotparser.RobotFileParser()
        try:
            resp = self._raw_get(
                f"{origin}/robots.txt", deadline, max_bytes=ROBOTS_MAX_BYTES, allowed_types=None, timeout_cap=5.0
            )
        except FetchFailure as exc:
            if exc.blocked:
                raise
            return None  # unreachable: complete disallow (RFC 9309 §2.3.1.4)
        if 400 <= resp.status < 500:
            rp.parse([])
            return rp
        if resp.status >= 500:
            return None
        rp.parse(resp.text.splitlines())
        return rp

    # ------------------------------------------------------------------ fetch

    def fetch(
        self,
        url: str,
        deadline: Deadline,
        *,
        max_bytes: int | None = None,
        allowed_types: tuple[str, ...] | None = None,
    ) -> FetchResult:
        cap = min(max_bytes or self.policy.max_response_bytes, self.policy.max_response_bytes)
        types = allowed_types if allowed_types is not None else self.policy.allowed_content_types
        current = self.check_scope(url)
        redirects: list[dict[str, Any]] = []
        for _hop in range(self.policy.max_redirects + 1):
            deadline.check()
            if not self.robots_allows(current, deadline):
                raise FetchFailure(ErrorCode.NETWORK_BLOCKED, f"robots.txt disallows {current}", blocked=True)
            result = self._raw_get(current, deadline, max_bytes=cap, allowed_types=types, follow=True)
            if 300 <= result.status < 400 and result.headers.get("location"):
                nxt = urljoin(current, result.headers["location"])
                redirects.append({"from": current, "status": result.status, "to": nxt})
                current = self.check_scope(nxt)
                continue
            result.requested_url = url
            result.redirects = redirects
            if result.status in (429, 503):
                ra = _retry_after(result.headers.get("retry-after"))
                raise FetchFailure(
                    ErrorCode.ENGINE_FAILURE,
                    f"destination is rate limiting (HTTP {result.status})",
                    retry_after=ra or 30.0,
                    status=result.status,
                )
            if result.status >= 400:
                raise FetchFailure(
                    ErrorCode.ENGINE_FAILURE,
                    f"destination returned HTTP {result.status}",
                    transport=result.status >= 500,
                    status=result.status,
                )
            return result
        raise FetchFailure(ErrorCode.NETWORK_BLOCKED, f"more than {self.policy.max_redirects} redirects", blocked=True)

    def _raw_get(
        self,
        url: str,
        deadline: Deadline,
        *,
        max_bytes: int,
        allowed_types: tuple[str, ...] | None,
        follow: bool = False,
        timeout_cap: float | None = None,
    ) -> FetchResult:
        deadline.check()
        remaining = deadline.remaining() if timeout_cap is None else min(deadline.remaining(), timeout_cap)
        timeout = httpx.Timeout(
            connect=min(self.policy.connect_timeout_ms / 1000, remaining),
            read=min(self.policy.read_timeout_ms / 1000, remaining),
            write=min(5.0, remaining),
            pool=min(2.0, remaining),
        )
        started = time.monotonic()
        self.stats.requests += 1
        try:
            with self.client.stream("GET", url, timeout=timeout) as resp:
                status = resp.status_code
                headers = {
                    k.lower(): v
                    for k, v in resp.headers.items()
                    if k.lower()
                    in ("location", "content-type", "retry-after", "etag", "last-modified", "content-encoding")
                }
                conns = self.transport.backend.connections
                peer = conns[-1].address if conns else None
                if follow and 300 <= status < 400:
                    return FetchResult(
                        url, url, status, "", "", "", 0, 0, False, datetime.now(UTC), headers=headers, peer_address=peer
                    )
                ctype = headers.get("content-type", "").split(";")[0].strip().lower()
                if allowed_types is not None and status < 400 and ctype not in allowed_types:
                    raise FetchFailure(
                        ErrorCode.CAPABILITY_UNSUPPORTED,
                        f"content type {ctype or 'unknown'} is not allowed",
                        blocked=True,
                        status=status,
                    )
                body, wire, truncated = self._read_body(resp, max_bytes, deadline, started)
        except FetchFailure:
            raise
        except EgressBlocked as exc:
            raise FetchFailure(ErrorCode.NETWORK_BLOCKED, str(exc), blocked=True) from exc
        except httpx.ConnectError as exc:
            cause = exc.__cause__ or exc.__context__
            if isinstance(cause, EgressBlocked) or "egress blocked" in str(exc):
                raise FetchFailure(ErrorCode.NETWORK_BLOCKED, str(exc), blocked=True) from exc
            raise FetchFailure(ErrorCode.ENGINE_FAILURE, f"connection failed: {exc}", transport=True) from exc
        except httpx.TimeoutException as exc:
            raise FetchFailure(ErrorCode.DEADLINE_EXCEEDED, f"timeout: {type(exc).__name__}", transport=True) from exc
        except httpx.HTTPError as exc:
            raise FetchFailure(ErrorCode.ENGINE_FAILURE, f"HTTP error: {type(exc).__name__}", transport=True) from exc
        self.stats.wire_bytes += wire
        if b"\x00" in body[:4096]:
            raise FetchFailure(
                ErrorCode.CAPABILITY_UNSUPPORTED,
                "response looks binary despite its content type",
                blocked=True,
                status=status,
            )
        charset = _charset(headers.get("content-type", ""), body)
        text = body.decode(charset, errors="replace")
        return FetchResult(
            url,
            url,
            status,
            ctype,
            charset,
            text,
            wire,
            len(body),
            truncated,
            datetime.now(UTC),
            headers=headers,
            peer_address=peer,
        )

    def _read_body(
        self, resp: httpx.Response, max_bytes: int, deadline: Deadline, started: float
    ) -> tuple[bytes, int, bool]:
        encoding = resp.headers.get("content-encoding", "identity").lower().strip()
        if encoding not in ("identity", "gzip", "x-gzip", "deflate", ""):
            raise FetchFailure(
                ErrorCode.CAPABILITY_UNSUPPORTED, f"unsupported content-encoding {encoding}", blocked=True
            )
        decomp = None
        if encoding in ("gzip", "x-gzip"):
            decomp = zlib.decompressobj(16 + zlib.MAX_WBITS)
        elif encoding == "deflate":
            decomp = zlib.decompressobj()
        wire = 0
        out = bytearray()
        truncated = False
        for raw in resp.iter_raw(chunk_size=16_384):
            deadline.check()
            wire += len(raw)
            if wire > max_bytes:
                truncated = True
                break
            chunk = decomp.decompress(raw, max(0, max_bytes - len(out)) + 1) if decomp else raw
            out += chunk
            if (
                decomp is not None
                and wire > 0
                and len(out) / wire > self.policy.max_decompression_ratio
                and len(out) > 64 * 1024
            ):
                raise FetchFailure(ErrorCode.PAYLOAD_TOO_LARGE, "decompression ratio limit exceeded", blocked=True)
            if len(out) > max_bytes:
                del out[max_bytes:]
                truncated = True
                break
        return bytes(out), wire, truncated


def _charset(content_type: str, body: bytes) -> str:
    m = re.search(r"charset=([\w.:-]+)", content_type, re.I)
    candidate = m.group(1) if m else None
    if candidate is None:
        mm = _CHARSET_META.search(body[:4096])
        candidate = mm.group(1).decode("ascii", "ignore") if mm else "utf-8"
    try:
        return codecs.lookup(candidate).name
    except LookupError:
        return "utf-8"


def _retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, min(float(value), 3600.0))
    except ValueError:
        return None
