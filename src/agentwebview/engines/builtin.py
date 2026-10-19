"""Two built-in engines that need no key.

* ``owned_corpus_search``: lexical full-text search over your collections, and reads of stored documents.
* ``static_html_reader``: reads one public page through the restricted egress layer and extracts its main
  text. Does not execute JavaScript; weak extractions fail the required quality checks, which lets the router
  fall back to a provider reader (Firecrawl, Jina) when one is connected.
"""

from __future__ import annotations

import hashlib
import time
from typing import Any

from agentwebview import documents
from agentwebview.contract import AnyTask, ReadPageTask, SearchTask
from agentwebview.egress import EgressPolicy
from agentwebview.engine import EngineDescriptor, EngineOutcome, Evidence, ExecutionContext, NetworkClass
from agentwebview.errors import GatewayError
from agentwebview.extract import RejectedImport, extract
from agentwebview.fetcher import Deadline, Fetcher, FetchFailure
from agentwebview.policy import Policy, host_allowed
from agentwebview.urls import UrlError, canonicalize, host_of

MAX_CONTENT_CHARS = 200_000
MAX_READ_CHARS = 200_000


def _host(url: str) -> str:
    try:
        return host_of(canonicalize(url))
    except UrlError:
        return ""


class OwnedCorpusSearch:
    def __init__(self, config: Any = None) -> None:
        self.config = config
        self.descriptor = EngineDescriptor(
            id="owned_corpus_search",
            name="Your documents (full-text search)",
            operations=frozenset({"search", "read_page"}),
            network_class=NetworkClass.LOCAL,
            coverage=frozenset({"owned_corpus", "stored"}),
            capabilities=frozenset({"lexical", "excerpts", "provenance", "stored_read"}),
            description="Searches only documents you imported or crawled into your collections. Not a web index; "
            "lexical ranking, no semantic retrieval.",
            default_latency_ms=30,
            min_deadline_ms=20,
        )

    def check_requirements(self, task: AnyTask, policy: Policy) -> list[str]:
        if isinstance(task, ReadPageTask) and task.input.source_id is None:
            return ["reads stored documents only (give source_id)"]
        return []

    def estimate_resources(self, task: AnyTask, policy: Policy) -> dict[str, int]:
        if task.operation == "search":
            return {"task_calls": 1, "search_calls": 1}
        return {"task_calls": 1}

    def execute(self, ctx: ExecutionContext) -> EngineOutcome:
        conn = ctx.app.db.read()
        task = ctx.task
        if isinstance(task, SearchTask):
            inp = task.input
            hits = documents.search(conn, inp.query, list(inp.collection_ids), ctx.max_results(), ctx.max_excerpt())
            evidence = []
            for h in hits:
                r = h["row"]
                evidence.append(
                    Evidence(
                        source_id=r["document_id"],
                        source_type=r["source_type"],
                        version_digest=r["content_digest"],
                        excerpt=h["excerpt"],
                        title=r["title"],
                        local_identifier=r["local_identifier"],
                        original_url=r["original_url"],
                        canonical_url=r["canonical_url"],
                        document_id=r["document_id"],
                        collection_id=r["collection_id"],
                        rank=h["score"],
                        fetched_at=r["fetched_at"],
                        indexed_at=r["updated_at"],
                        imported_at=r["created_at"],
                        published_at=r["published_at"],
                        extraction_method=r["extraction_method"],
                        content_chars=r["chars"],
                    )
                )
            names = [c["name"] for c in documents.list_collections(conn) if not inp.collection_ids or c["id"] in inp.collection_ids]
            return EngineOutcome(
                ok=True,
                evidence=evidence,
                usage={"task_calls": 1, "search_calls": 1},
                coverage={
                    "kind": "owned_corpus",
                    "collections": names,
                    "results": len(evidence),
                    "statement": "Results come only from your own collections"
                    + (f" ({', '.join(names[:10])})" if names else " (none yet)")
                    + ". This is not a web search; ask with scope 'web' to search the web through your providers.",
                },
            )
        assert isinstance(task, ReadPageTask) and task.input.source_id is not None
        row = documents.read_document(conn, task.input.source_id)
        text = str(row["body"])
        content = text[:MAX_READ_CHARS]
        ev = Evidence(
            source_id=row["id"],
            source_type=row["source_type"],
            version_digest=row["content_digest"],
            excerpt=" ".join(content.split())[: ctx.max_excerpt()],
            title=row["title"],
            local_identifier=row["local_identifier"],
            original_url=row["original_url"],
            canonical_url=row["canonical_url"],
            document_id=row["id"],
            collection_id=row["collection_id"],
            fetched_at=row["fetched_at"],
            indexed_at=row["updated_at"],
            imported_at=row["created_at"],
            extraction_method=row["extraction_method"],
            truncated=len(text) > MAX_READ_CHARS,
            content_chars=len(text),
        )
        return EngineOutcome(
            ok=True,
            evidence=[ev],
            usage={"task_calls": 1},
            content=content,
            partial=ev.truncated,
            coverage={"kind": "stored", "statement": "Read from a stored copy in your collections; no network request."},
        )


class StaticHtmlReader:
    def __init__(self, config: Any) -> None:
        self.config = config
        self.descriptor = EngineDescriptor(
            id="static_html_reader",
            name="Built-in page reader",
            operations=frozenset({"read_page"}),
            network_class=NetworkClass.DIRECT_WEB,
            coverage=frozenset({"live_destination"}),
            capabilities=frozenset({"static_html", "extraction", "robots"}),
            description="Fetches one public page through the restricted egress layer (private networks are "
            "refused) and extracts its main text. Respects robots.txt. Does not execute JavaScript.",
            default_latency_ms=800,
            min_deadline_ms=300,
        )

    def check_requirements(self, task: AnyTask, policy: Policy) -> list[str]:
        if not isinstance(task, ReadPageTask):
            return [f"does not support operation '{task.operation}'"]
        if task.input.url is None:
            return ["needs a URL (stored documents are read by owned_corpus_search)"]
        if not host_allowed(policy.direct_web, _host(task.input.url)):
            return ["destination host is not permitted by policy"]
        return []

    def estimate_resources(self, task: AnyTask, policy: Policy) -> dict[str, int]:
        # robots.txt + the page + every redirect hop may each be a request.
        return {"task_calls": 1, "fetched_pages": 2 + policy.direct_web.max_redirects}

    def execute(self, ctx: ExecutionContext) -> EngineOutcome:
        assert isinstance(ctx.task, ReadPageTask)
        url = ctx.task.input.url or ""
        egress = EgressPolicy.from_config(self.config)
        deadline = Deadline(ctx.remaining_ms() / 1000, ctx.cancel)
        with Fetcher(ctx.policy.direct_web, egress) as f:
            try:
                res = f.fetch(url, deadline)
            except FetchFailure as exc:
                return EngineOutcome(
                    ok=False,
                    usage={"task_calls": 1, "fetched_pages": f.stats.requests},
                    error=exc.as_gateway_error(),
                    transport_error=exc.transport,
                    rate_limited_until=time.time() + exc.retry_after if exc.retry_after else None,
                    coverage={"kind": "live_destination", "url": url, "host": _host(url)},
                    detail={"blocked": exc.blocked, "status": exc.status},
                )
            usage = {"task_calls": 1, "fetched_pages": f.stats.requests}
        name = "page.html" if res.content_type in ("text/html", "application/xhtml+xml") else "page.txt"
        if res.content_type == "text/markdown":
            name = "page.md"
        try:
            ex = extract(
                name, res.text.encode("utf-8"), declared_media_type=res.content_type, original_url=res.final_url,
                max_chars=MAX_CONTENT_CHARS,
            )
        except RejectedImport as exc:
            return EngineOutcome(
                ok=True,
                usage=usage,
                content="",
                coverage={"kind": "live_destination", "url": res.final_url, "host": _host(res.final_url)},
                detail={"extraction": f"rejected: {exc}"},
            )
        canonical = ex.canonical_url or res.final_url
        ev = Evidence(
            source_id="url:" + hashlib.sha256(canonical.encode()).hexdigest()[:24],
            source_type="live_page",
            version_digest=ex.content_digest,
            excerpt=ex.text[: ctx.max_excerpt()],
            title=ex.title,
            original_url=url,
            canonical_url=canonical,
            fetched_at=res.fetched_at.isoformat(),
            published_at=ex.published_at.isoformat() if ex.published_at else None,
            extraction_method=ex.method,
            truncated=res.truncated or ex.truncated,
            content_chars=len(ex.text),
        )
        return EngineOutcome(
            ok=True,
            evidence=[ev],
            usage=usage,
            content=ex.text,
            partial=ev.truncated,
            coverage={
                "kind": "live_destination",
                "url": res.final_url,
                "host": _host(res.final_url),
                "redirects": res.redirects,
                "http_status": res.status,
                "content_type": res.content_type,
                "statement": "Content was fetched live from the requested destination only "
                "(its address was checked to be public at connection time).",
            },
            detail={"bytes": res.body_bytes, "wire_bytes": res.wire_bytes, "charset": res.charset},
        )


def validate_live_url(policy: Policy, url: str) -> None:
    """Fail fast with NETWORK_BLOCKED for destinations outside the policy."""
    from agentwebview.errors import ErrorCode

    try:
        canon = canonicalize(url)
    except UrlError as exc:
        raise GatewayError(ErrorCode.VALIDATION_FAILED, f"invalid URL: {exc}") from exc
    if not host_allowed(policy.direct_web, host_of(canon)):
        raise GatewayError(
            ErrorCode.NETWORK_BLOCKED,
            f"destination host {host_of(canon)} is not permitted by policy",
            remediation="allow every public host, or add this one, under Settings > Reading websites",
        )
