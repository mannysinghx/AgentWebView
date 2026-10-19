"""Restricted egress for every outbound fetch the gateway makes to a website.

Enforcement happens when a TCP connection is opened, not on URL strings:

1. The hostname is resolved exactly once per connection.
2. Every resolved address must be a public unicast address; if any answer is private, loopback,
   link-local (including cloud metadata 169.254.169.254), multicast, reserved, CGNAT, IPv4-mapped,
   6to4, Teredo or NAT64, the connection is refused (mixed answers are treated as an attack).
3. The socket is dialled to that validated address, never re-resolved, which defeats DNS rebinding
   between validation and connection. TLS still verifies the certificate against the hostname.
4. Only allow-listed ports are reachable; non-HTTP schemes never reach this layer.

Redirects are followed manually by the fetcher, so each hop gets a fresh connection-time check.
The browser uses the same ``resolve_and_validate`` through its policy proxy.
"""

from __future__ import annotations

import ipaddress
import socket
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

import httpcore
import httpx

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
DEFAULT_PORTS = frozenset({80, 443, 8080, 8443})
NAT64 = ipaddress.ip_network("64:ff9b::/96")
NAT64_LOCAL = ipaddress.ip_network("64:ff9b:1::/48")


class EgressBlocked(httpcore.ConnectError):
    """Connection refused by egress policy (a ConnectError so httpx surfaces it cleanly)."""

    def __init__(self, reason: str, host: str, address: str | None = None) -> None:
        super().__init__(f"egress blocked: {reason} ({host}{' -> ' + address if address else ''})")
        self.reason = reason
        self.host = host
        self.address = address


def check_address(ip: IPAddress) -> str | None:
    """Return a refusal reason, or None when ``ip`` is an acceptable public unicast destination."""
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return "IPv4-mapped IPv6 address"
        if ip.sixtofour is not None:
            return "6to4 address (embeds IPv4)"
        if ip.teredo is not None:
            return "Teredo address (embeds IPv4)"
        if ip in NAT64 or ip in NAT64_LOCAL:
            return "NAT64 address (embeds IPv4)"
        if ip.is_site_local:
            return "IPv6 site-local address"
    if ip.is_loopback:
        return "loopback address"
    if ip.is_link_local:
        return "link-local address (includes cloud metadata endpoints)"
    if ip.is_multicast:
        return "multicast address"
    if ip.is_unspecified:
        return "unspecified address"
    if ip.is_private:
        return "private address"
    if ip.is_reserved:
        return "reserved address"
    if not ip.is_global:
        return "non-global address"
    return None


Resolver = Callable[[str, int], list[str]]


def system_resolver(host: str, port: int) -> list[str]:
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP)
    seen: list[str] = []
    for _family, _type, _proto, _canon, sockaddr in infos:
        addr = str(sockaddr[0]).split("%", 1)[0]
        if addr not in seen:
            seen.append(addr)
    return seen


@dataclass
class EgressPolicy:
    allowed_ports: frozenset[int] = DEFAULT_PORTS
    resolver: Resolver = system_resolver
    # Test-only: map a validated "ip:port" to the real fixture socket address "127.0.0.1:port".
    dial_overrides: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_config(cls, config: Any) -> EgressPolicy:
        if not getattr(config, "test_mode", False) or not config.test_dns:
            return cls()
        overrides = {k.lower(): list(v) for k, v in config.test_dns.items()}

        def resolver(host: str, port: int) -> list[str]:
            key = host.lower().rstrip(".")
            if key in overrides:
                return list(overrides[key])
            raise socket.gaierror(f"test resolver has no record for {host}")

        return cls(resolver=resolver, dial_overrides=dict(config.test_dial))


def resolve_and_validate(policy: EgressPolicy, host: str, port: int) -> tuple[str, str, int]:
    """Resolve once, validate every answer, and return (validated_ip, dial_host, dial_port)."""
    if port not in policy.allowed_ports:
        raise EgressBlocked(f"port {port} is not allowed", host)
    try:
        literal: IPAddress | None = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        literal = None
    if literal is not None:
        addresses = [str(literal)]
    else:
        try:
            addresses = policy.resolver(host, port)
        except (OSError, UnicodeError) as exc:
            raise httpcore.ConnectError(f"DNS resolution failed for {host}: {type(exc).__name__}") from exc
        if not addresses:
            raise httpcore.ConnectError(f"DNS returned no addresses for {host}")
    parsed: list[IPAddress] = []
    for a in addresses:
        ip = ipaddress.ip_address(a)
        reason = check_address(ip)
        if reason:
            raise EgressBlocked(reason, host, a)
        parsed.append(ip)
    chosen = str(parsed[0])
    override = policy.dial_overrides.get(f"{chosen}:{port}")
    if override:
        h, p = override.rsplit(":", 1)
        return chosen, h, int(p)
    return chosen, chosen, port


@dataclass
class ConnectionRecord:
    host: str
    port: int
    address: str


class GuardedBackend(httpcore.NetworkBackend):
    def __init__(self, policy: EgressPolicy) -> None:
        self.policy = policy
        self._inner = httpcore.SyncBackend()
        self._lock = threading.Lock()
        self.connections: list[ConnectionRecord] = []

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.NetworkStream:
        chosen, target_host, target_port = resolve_and_validate(self.policy, host, port)
        stream = self._inner.connect_tcp(
            target_host, target_port, timeout=timeout, local_address=local_address, socket_options=socket_options
        )
        with self._lock:
            self.connections.append(ConnectionRecord(host, port, chosen))
        return stream

    def connect_unix_socket(
        self, path: str, timeout: float | None = None, socket_options: Iterable[Any] | None = None
    ) -> httpcore.NetworkStream:
        raise EgressBlocked("unix sockets are not permitted", path)

    def sleep(self, seconds: float) -> None:
        self._inner.sleep(seconds)


class GuardedTransport(httpx.HTTPTransport):
    """HTTPX transport whose every TCP connection passes through ``GuardedBackend``."""

    def __init__(self, policy: EgressPolicy, *, verify: Any = True) -> None:
        super().__init__(verify=verify, trust_env=False, http1=True, http2=False, retries=0)
        self.backend = GuardedBackend(policy)
        ssl_context = self._pool._ssl_context  # reuse httpx's verified context
        self._pool = httpcore.ConnectionPool(
            ssl_context=ssl_context,
            max_connections=8,
            max_keepalive_connections=4,
            keepalive_expiry=5.0,
            network_backend=self.backend,
        )
