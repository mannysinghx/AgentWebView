"""Policy proxy for browser traffic.

Chromium is started with ``--proxy-server`` pointing here and ``--proxy-bypass-list=<-loopback>`` so that
every request (top-level navigations, subresources, frames, fetch/XHR, WebSockets via CONNECT, downloads)
reaches this proxy, including requests to localhost. For each request the proxy checks the session's host
scope and then applies the same connection-time rules as the page reader (``resolve_and_validate``: resolve
once, refuse non-public answers, dial the validated address). The proxy runs in the AgentWebView process, not
in the browser process.
"""

from __future__ import annotations

import contextlib
import select
import socket
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import httpcore

from agentwebview.egress import EgressBlocked, EgressPolicy, resolve_and_validate

MAX_HEAD = 16 * 1024


@dataclass
class ProxyStats:
    requests: int = 0
    bytes_up: int = 0
    bytes_down: int = 0
    allowed: list[dict[str, Any]] = field(default_factory=list)
    blocked: list[dict[str, Any]] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def record(self, kind: str, **entry: Any) -> None:
        with self.lock:
            target = self.blocked if kind == "blocked" else self.allowed
            if len(target) < 2_000:
                target.append({"at": time.time(), **entry})


class PolicyProxy:
    def __init__(
        self, egress: EgressPolicy, allow_host: Callable[[str], bool], *, max_bytes: int, idle_timeout: float = 30.0
    ) -> None:
        self.egress = egress
        self.allow_host = allow_host
        self.max_bytes = max_bytes
        self.idle_timeout = idle_timeout
        self.stats = ProxyStats()
        self._sock: socket.socket | None = None
        self._stop = threading.Event()
        self.port = 0

    def start(self) -> int:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", 0))
        s.listen(128)
        s.settimeout(0.2)
        self._sock = s
        self.port = s.getsockname()[1]
        threading.Thread(target=self._accept_loop, name="agentwebview-proxy", daemon=True).start()
        return self.port

    def stop(self) -> None:
        self._stop.set()
        if self._sock is not None:
            with contextlib.suppress(OSError):
                self._sock.close()

    def total_bytes(self) -> int:
        return self.stats.bytes_up + self.stats.bytes_down

    def _accept_loop(self) -> None:
        assert self._sock is not None
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except (TimeoutError, OSError):
                continue
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _read_head(self, conn: socket.socket) -> tuple[bytes, bytes]:
        conn.settimeout(10)
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = conn.recv(4096)
            if not chunk:
                break
            buf += chunk
            if len(buf) > MAX_HEAD:
                raise ValueError("request head too large")
        head, _, rest = buf.partition(b"\r\n\r\n")
        return head, rest

    def _deny(self, conn: socket.socket, status: int, reason: str, **entry: Any) -> None:
        self.stats.record("blocked", reason=reason, **entry)
        body = f"blocked by AgentWebView egress policy: {reason}\n".encode()
        with contextlib.suppress(OSError):
            conn.sendall(
                f"HTTP/1.1 {status} Blocked\r\nContent-Type: text/plain\r\nContent-Length: {len(body)}\r\n"
                f"Connection: close\r\nX-AgentWebView-Egress: blocked\r\n\r\n".encode()
                + body
            )

    def _handle(self, conn: socket.socket) -> None:
        upstream: socket.socket | None = None
        try:
            try:
                head, rest = self._read_head(conn)
            except (ValueError, OSError):
                return
            if not head:
                return
            lines = head.split(b"\r\n")
            try:
                method, target, version = lines[0].decode("latin-1").split(" ", 2)
            except ValueError:
                return self._deny(conn, 400, "malformed request line")
            with self.stats.lock:
                self.stats.requests += 1
            if self.total_bytes() >= self.max_bytes:
                return self._deny(conn, 429, "session byte budget exhausted", target=target)
            if method.upper() == "CONNECT":
                host, _, port_s = target.rpartition(":")
                host = host.strip("[]").lower()
                port = int(port_s) if port_s.isdigit() else 443
                upstream = self._dial(conn, host, port, target)
                if upstream is None:
                    return None
                conn.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                if rest:
                    upstream.sendall(rest)
                return self._pipe(conn, upstream)
            parts = urlsplit(target)
            if parts.scheme not in ("http", "ws") or not parts.hostname:
                return self._deny(conn, 400, "only absolute http:// requests or CONNECT are accepted", target=target)
            host = parts.hostname.lower()
            port = parts.port or 80
            upstream = self._dial(conn, host, port, f"{parts.scheme}://{host}:{port}")
            if upstream is None:
                return None
            path = parts.path or "/"
            if parts.query:
                path += "?" + parts.query
            out = [f"{method} {path} {version}".encode("latin-1")]
            for line in lines[1:]:
                name = line.split(b":", 1)[0].strip().lower()
                if name in (b"proxy-connection", b"proxy-authorization", b"connection", b"keep-alive"):
                    continue
                out.append(line)
            upgrade = any(ln.lower().startswith(b"upgrade:") for ln in lines[1:])
            out.append(b"Connection: " + (b"Upgrade" if upgrade else b"close"))
            upstream.sendall(b"\r\n".join(out) + b"\r\n\r\n" + rest)
            self.stats.bytes_up += len(rest)
            return self._pipe(conn, upstream)
        finally:
            for s in (conn, upstream):
                if s is not None:
                    with contextlib.suppress(OSError):
                        s.close()

    def _dial(self, conn: socket.socket, host: str, port: int, target: str) -> socket.socket | None:
        if not self.allow_host(host):
            self._deny(conn, 403, "host outside session scope", host=host, port=port, target=target)
            return None
        try:
            ip, dial_host, dial_port = resolve_and_validate(self.egress, host, port)
        except EgressBlocked as exc:
            self._deny(conn, 403, exc.reason, host=host, port=port, address=exc.address, target=target)
            return None
        except httpcore.ConnectError as exc:
            self._deny(conn, 502, f"resolution failed: {exc}", host=host, port=port, target=target)
            return None
        try:
            up = socket.create_connection((dial_host, dial_port), timeout=10)
        except OSError as exc:
            self._deny(conn, 502, f"connect failed: {type(exc).__name__}", host=host, port=port, target=target)
            return None
        self.stats.record("allowed", host=host, port=port, address=ip)
        return up

    def _pipe(self, a: socket.socket, b: socket.socket) -> None:
        a.setblocking(False)
        b.setblocking(False)
        sockets = [a, b]
        last = time.monotonic()
        while not self._stop.is_set():
            readable, _, errored = select.select(sockets, [], sockets, 0.5)
            if errored:
                return
            if not readable:
                if time.monotonic() - last > self.idle_timeout:
                    return
                continue
            for s in readable:
                try:
                    data = s.recv(65_536)
                except (BlockingIOError, InterruptedError):
                    continue
                except OSError:
                    return
                if not data:
                    return
                other = b if s is a else a
                with self.stats.lock:
                    if s is a:
                        self.stats.bytes_up += len(data)
                    else:
                        self.stats.bytes_down += len(data)
                if self.total_bytes() > self.max_bytes:
                    self.stats.record("blocked", reason="session byte budget exhausted mid-stream")
                    return
                try:
                    other.setblocking(True)
                    other.sendall(data)
                    other.setblocking(False)
                except OSError:
                    return
                last = time.monotonic()
