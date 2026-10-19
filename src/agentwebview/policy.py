"""The rules every call follows, edited on the Settings screen and stored in the database.

There is one policy for the whole installation (no workspaces or projects). Each agent then has its own
abilities (which tools it may use) and limits (requests per month and per day).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

RoutingMode = Literal["economical", "balanced", "quality"]
BrowserWritePolicy = Literal["deny", "require_decision", "allow_with_capability"]


class _Doc(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CachePolicy(_Doc):
    enabled: bool = True
    ttl_seconds: int = Field(default=3600, ge=0, le=30 * 86400)
    # How long an expired entry may still be served when a caller asks for cache_mode "use_cache".
    stale_seconds: int = Field(default=86_400, ge=0, le=90 * 86400)


class DirectWebPolicy(_Doc):
    """Reading websites directly (the built-in page reader and the crawler). Private networks are always refused."""

    enabled: bool = True
    allow_any_public_host: bool = True
    allowed_hosts: tuple[str, ...] = ()
    max_response_bytes: int = Field(default=2_000_000, ge=1_024, le=50_000_000)
    max_redirects: int = Field(default=5, ge=0, le=10)
    connect_timeout_ms: int = Field(default=5_000, ge=100, le=60_000)
    read_timeout_ms: int = Field(default=10_000, ge=100, le=120_000)
    max_decompression_ratio: int = Field(default=50, ge=1, le=1_000)
    allowed_content_types: tuple[str, ...] = ("text/html", "application/xhtml+xml", "text/plain", "text/markdown")
    respect_robots: bool = True


class CrawlPolicy(_Doc):
    max_pages: int = Field(default=200, ge=1, le=100_000)
    max_depth: int = Field(default=3, ge=0, le=20)
    max_total_bytes: int = Field(default=100_000_000, ge=1_024)
    max_duration_ms: int = Field(default=30 * 60_000, ge=1_000)
    min_host_delay_ms: int = Field(default=1_000, ge=0, le=60_000)


class BrowserPolicy(_Doc):
    enabled: bool = False
    max_sessions: int = Field(default=2, ge=0, le=16)
    max_session_ms: int = Field(default=5 * 60_000, ge=1_000, le=60 * 60_000)
    # Clicks that could submit, send, buy, delete or publish: refuse them, ask a person, or let an agent
    # that was given the capability go ahead.
    write_actions: BrowserWritePolicy = "require_decision"
    screenshots: bool = True
    # How long an action waits for a person's decision before it expires.
    decision_timeout_s: int = Field(default=600, ge=10, le=86_400)


class Policy(_Doc):
    routing_mode: RoutingMode = "economical"
    # "round_robin": web searches use the least recently used provider key first, so searches spread across
    # every provider with credit left. "score": the best-scoring key first, the others only as fallback.
    search_rotation: Literal["score", "round_robin"] = "score"
    providers_enabled: bool = True
    max_attempts: int = Field(default=2, ge=1, le=5)
    max_results: int = Field(default=20, ge=1, le=200)
    max_excerpt_chars: int = Field(default=600, ge=50, le=5_000)
    # Calendar months and days for limits are counted in this time zone.
    timezone: str = Field(default="UTC", max_length=64)
    cache: CachePolicy = CachePolicy()
    direct_web: DirectWebPolicy = DirectWebPolicy()
    crawl: CrawlPolicy = CrawlPolicy()
    browser: BrowserPolicy = BrowserPolicy()


def host_allowed(policy: DirectWebPolicy, host: str) -> bool:
    return policy.allow_any_public_host or host in policy.allowed_hosts
