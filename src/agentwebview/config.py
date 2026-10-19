"""Process configuration: where data lives and how the server listens.

Everything a person changes while using AgentWebView (routing, which tools are on, limits) is stored in
the database and edited in the dashboard; see ``policy.py``. This file only holds what must be known
before the database is opened.
"""

from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass, field
from pathlib import Path


def _default_data_dir() -> Path:
    return Path(os.environ.get("AGENTWEBVIEW_DATA", str(Path.home() / ".agentwebview"))).expanduser()


def is_loopback(host: str) -> bool:
    if host in ("localhost", ""):
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


@dataclass
class Config:
    data_dir: Path = field(default_factory=_default_data_dir)
    host: str = "127.0.0.1"
    port: int = 8718
    # Binding to anything other than loopback exposes a dashboard that has no login; it must be asked for.
    allow_remote: bool = False
    # Extra Host header values the server answers to (for example a name in your own network).
    extra_hosts: tuple[str, ...] = ()
    # The address agents use to reach this server, shown in the connection instructions.
    public_url: str | None = None
    provider_timeout_ms: int = 20_000
    provider_max_response_bytes: int = 5_000_000
    # Test hooks: honoured only when test_mode is on.
    test_mode: bool = False
    test_dns: dict[str, list[str]] = field(default_factory=dict)
    test_dial: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_env(cls, **overrides: object) -> Config:
        env = os.environ
        cfg = cls(
            data_dir=_default_data_dir(),
            host=env.get("AGENTWEBVIEW_HOST", "127.0.0.1"),
            port=int(env.get("AGENTWEBVIEW_PORT", "8718")),
            allow_remote=env.get("AGENTWEBVIEW_ALLOW_REMOTE", "") in ("1", "true", "yes"),
            extra_hosts=tuple(h.strip() for h in env.get("AGENTWEBVIEW_EXTRA_HOSTS", "").split(",") if h.strip()),
            public_url=env.get("AGENTWEBVIEW_PUBLIC_URL") or None,
        )
        for key, value in overrides.items():
            if value is not None:
                setattr(cfg, key, value)
        return cfg

    @property
    def db_path(self) -> Path:
        return self.data_dir / "agentwebview.db"

    @property
    def key_path(self) -> Path:
        return self.data_dir / "secret.key"

    @property
    def artifacts_dir(self) -> Path:
        return self.data_dir / "screenshots"

    @property
    def base_url(self) -> str:
        if self.public_url:
            return self.public_url.rstrip("/")
        host = "127.0.0.1" if self.host in ("0.0.0.0", "::", "") else self.host  # noqa: S104
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        return f"http://{host}:{self.port}"

    def allowed_host_headers(self) -> set[str]:
        """Host header values accepted by the server (DNS-rebinding protection)."""
        names = {"localhost", "127.0.0.1", "[::1]", *self.extra_hosts}
        if self.public_url:
            from urllib.parse import urlsplit

            parts = urlsplit(self.public_url)
            if parts.hostname:
                names.add(parts.hostname)
        if not is_loopback(self.host) and self.host not in ("0.0.0.0", "::"):  # noqa: S104
            names.add(self.host)
        return names

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.data_dir, 0o700)
        except OSError:
            pass
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
