"""The task contract shared by the REST API, the MCP tools and the dashboard's playground.

Requests are validated into operation-specific models (discriminated on ``operation``). Unknown fields
are rejected everywhere, so a caller cannot smuggle provider parameters past the rules.
"""

from __future__ import annotations

import hashlib
import json
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, field_validator, model_validator

from agentwebview.errors import ErrorCode, GatewayError

CacheMode = Literal["prefer_fresh", "use_cache", "bypass", "cache_only"]
# owned_corpus: your own documents. stored: a stored document read back. live_destination: the page itself.
# web_index: a connected provider's web index (never described as complete or global coverage).
Coverage = Literal["owned_corpus", "stored", "live_destination", "web_index"]
RoutingModeArg = Literal["economical", "balanced", "quality", "pinned"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Limits(_Strict):
    deadline_ms: int = Field(default=20_000, ge=1_000, le=120_000)
    max_attempts: int | None = Field(default=None, ge=1, le=5)
    max_results: int = Field(default=8, ge=1, le=200)
    max_excerpt_chars: int = Field(default=400, ge=50, le=5_000)


class CallerPolicy(_Strict):
    cache_mode: CacheMode = "prefer_fresh"
    routing_mode: RoutingModeArg | None = None
    pinned_engine: str | None = Field(default=None, max_length=64)
    allowed_engines: list[str] | None = Field(default=None, max_length=16)

    @model_validator(mode="after")
    def _pinned(self) -> CallerPolicy:
        if self.routing_mode == "pinned" and not self.pinned_engine:
            raise ValueError("routing_mode 'pinned' requires pinned_engine")
        if self.pinned_engine and self.routing_mode not in (None, "pinned"):
            raise ValueError("pinned_engine requires routing_mode 'pinned' (or omit routing_mode)")
        return self


class Requirements(_Strict):
    coverage: Coverage | None = None
    max_age_seconds: int | None = Field(default=None, ge=0, le=10 * 365 * 86400)
    capabilities: list[str] = Field(default_factory=list, max_length=16)
    min_results: int = Field(default=1, ge=0, le=200)
    # Quality floor (0-1): an engine whose observed quality here is known and below it is not used, so a
    # fallback cannot silently downgrade results. Unknown quality is allowed.
    min_quality: float | None = Field(default=None, ge=0.0, le=1.0)


class SearchInput(_Strict):
    query: str = Field(min_length=1, max_length=1_000)
    collection_ids: list[str] = Field(default_factory=list, max_length=50)

    @field_validator("query")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = " ".join(v.split())
        if not v:
            raise ValueError("query must contain non-whitespace characters")
        return v


class ReadPageInput(_Strict):
    url: str | None = Field(default=None, max_length=2_048)
    source_id: str | None = Field(default=None, max_length=64)

    @model_validator(mode="after")
    def _one(self) -> ReadPageInput:
        if (self.url is None) == (self.source_id is None):
            raise ValueError("provide exactly one of url or source_id")
        return self


class CrawlInput(_Strict):
    collection_id: str = Field(min_length=1, max_length=64)
    seeds: list[str] = Field(default_factory=list, max_length=100)
    sitemaps: list[str] = Field(default_factory=list, max_length=20)
    allowed_hosts: list[str] = Field(default_factory=list, max_length=50)
    exact_urls_only: bool = False
    max_pages: int = Field(default=50, ge=1, le=100_000)
    max_depth: int = Field(default=2, ge=0, le=20)
    max_duration_ms: int = Field(default=10 * 60_000, ge=1_000, le=6 * 3_600_000)

    @model_validator(mode="after")
    def _seeded(self) -> CrawlInput:
        if not self.seeds and not self.sitemaps:
            raise ValueError("a crawl needs at least one seed URL or sitemap")
        return self


class _TaskBase(_Strict):
    requirements: Requirements = Requirements()
    limits: Limits = Limits()
    policy: CallerPolicy = CallerPolicy()


class SearchTask(_TaskBase):
    operation: Literal["search"]
    input: SearchInput


class ReadPageTask(_TaskBase):
    operation: Literal["read_page"]
    input: ReadPageInput


class CrawlTask(_TaskBase):
    operation: Literal["crawl"]
    input: CrawlInput


AnyTask = SearchTask | ReadPageTask | CrawlTask
TASK_ADAPTER: TypeAdapter[Any] = TypeAdapter(Annotated[AnyTask, Field(discriminator="operation")])

DEFAULT_COVERAGE: dict[str, str] = {
    "search": "owned_corpus",
    "read_page": "live_destination",
    "crawl": "live_destination",
}
SUPPORTED_COVERAGE: dict[str, frozenset[str]] = {
    "search": frozenset({"owned_corpus", "web_index"}),
    "read_page": frozenset({"stored", "live_destination"}),
    "crawl": frozenset({"live_destination"}),
}


def parse_task(data: Any) -> AnyTask:
    """Validate a task request; invalid input is a 422 with the first few field errors."""
    try:
        task: AnyTask = TASK_ADAPTER.validate_python(data)
    except ValidationError as exc:
        raise GatewayError(
            ErrorCode.VALIDATION_FAILED,
            "invalid task request",
            details={"errors": [f"{'.'.join(map(str, e['loc']))}: {e['msg']}"[:200] for e in exc.errors()[:10]]},
        ) from exc
    cov = task.requirements.coverage
    if cov is not None and cov not in SUPPORTED_COVERAGE[task.operation]:
        raise GatewayError(
            ErrorCode.VALIDATION_FAILED,
            f"coverage '{cov}' is not supported for {task.operation}; "
            f"use one of: {', '.join(sorted(SUPPORTED_COVERAGE[task.operation]))}",
        )
    return task


def coverage_of(task: AnyTask) -> str:
    if task.requirements.coverage:
        return task.requirements.coverage
    if isinstance(task, ReadPageTask) and task.input.source_id is not None:
        return "stored"
    return DEFAULT_COVERAGE[task.operation]


def request_hash(task: AnyTask) -> str:
    blob = json.dumps(task.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def summary_of(task: AnyTask) -> str:
    if isinstance(task, SearchTask):
        return task.input.query
    if isinstance(task, ReadPageTask):
        return task.input.url or f"document {task.input.source_id}"
    return ", ".join(task.input.seeds or task.input.sitemaps)[:300]
