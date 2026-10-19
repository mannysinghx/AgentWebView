"""scoped_crawler: policy-bound crawling into one of your collections.

Every page is reserved before it is fetched and settled right after, so a crawl can never spend beyond its
limits; the moment a reservation is refused the crawl stops with ``budget_exhausted``. Scope never widens:
links are followed only to hosts in the crawl's scope (the seed hosts unless you name others). robots.txt
is always respected, and each host gets at least the policy's minimum delay between requests (longer when
robots.txt asks for it). Completed pages are stored as they finish, so cancelling keeps them.
"""

from __future__ import annotations

import time
from collections import deque
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urljoin, urlsplit

from agentwebview import documents
from agentwebview.contract import AnyTask, CrawlInput, CrawlTask
from agentwebview.db import dumps, iso
from agentwebview.egress import EgressPolicy
from agentwebview.engine import EngineDescriptor, EngineOutcome, Evidence, ExecutionContext, NetworkClass
from agentwebview.errors import ErrorCode, GatewayError
from agentwebview.extract import RejectedImport, extract
from agentwebview.fetcher import Deadline, Fetcher, FetchFailure
from agentwebview.policy import Policy, host_allowed
from agentwebview.urls import UrlError, canonicalize, host_of

SITEMAP_MAX_BYTES = 2_000_000
MAX_LINKS_PER_PAGE = 500
MAX_SITEMAP_CHILDREN = 20
MAX_HOST_BACKOFF_S = 120.0


def crawl_scope(inp: CrawlInput) -> set[str]:
    hosts = {h.strip().lower().rstrip(".") for h in inp.allowed_hosts if h.strip()}
    if not hosts:
        for u in list(inp.seeds) + list(inp.sitemaps):
            try:
                hosts.add(host_of(canonicalize(u)))
            except UrlError:
                continue
    return hosts


def _links(html: str, base: str) -> list[str]:
    from lxml import html as lxml_html

    try:
        tree = lxml_html.fromstring(html)
    except Exception:
        return []
    base_el = tree.find(".//base[@href]")
    base_url = urljoin(base, base_el.get("href")) if base_el is not None else base
    out: list[str] = []
    seen: set[str] = set()
    for a in tree.xpath("//a[@href]"):
        rel = (a.get("rel") or "").lower()
        if "nofollow" in rel:
            continue
        href = (a.get("href") or "").strip()
        if not href or href.startswith(("javascript:", "mailto:", "tel:", "data:", "#")):
            continue
        absolute = urljoin(base_url, href)
        if absolute not in seen:
            seen.add(absolute)
            out.append(absolute)
    return out


def _parse_sitemap(xml_text: str) -> tuple[list[str], list[str]]:
    """Parse a sitemap or sitemap index without entity expansion or network access (XXE-safe)."""
    from lxml import etree

    parser = etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=False, load_dtd=False)
    try:
        root = etree.fromstring(xml_text.encode("utf-8"), parser=parser)
    except etree.XMLSyntaxError:
        return [], []
    tag = etree.QName(root).localname
    locs = [
        el.text.strip()
        for el in root.iter()
        if isinstance(el.tag, str) and etree.QName(el).localname == "loc" and el.text
    ]
    if tag == "sitemapindex":
        return [], locs
    return locs, []


class ScopedCrawler:
    def __init__(self, config: Any) -> None:
        self.config = config
        self.descriptor = EngineDescriptor(
            id="scoped_crawler",
            name="Scoped crawler (seeds, sitemaps, robots.txt)",
            operations=frozenset({"crawl"}),
            network_class=NetworkClass.DIRECT_WEB,
            coverage=frozenset({"live_destination"}),
            capabilities=frozenset({"crawl", "robots", "sitemaps"}),
            description="Crawls public pages within the seed hosts into one of your collections, page by page "
            "within your limits.",
            replay_safe=False,
            default_latency_ms=5_000,
            min_deadline_ms=1_000,
        )

    def check_requirements(self, task: AnyTask, policy: Policy) -> list[str]:
        if not isinstance(task, CrawlTask):
            return [f"does not support operation '{task.operation}'"]
        hosts = crawl_scope(task.input)
        if not hosts:
            return ["no valid seed host"]
        blocked = sorted(h for h in hosts if not host_allowed(policy.direct_web, h))
        if blocked:
            return [f"hosts not permitted by policy: {', '.join(blocked[:5])}"]
        return []

    def estimate_resources(self, task: AnyTask, policy: Policy) -> dict[str, int]:
        return {"task_calls": 1}

    def probe_resources(self, task: AnyTask, policy: Policy) -> dict[str, int]:
        return {"task_calls": 1, "fetched_pages": 2}

    def cost_hint(self, task: AnyTask, policy: Policy) -> dict[str, int]:
        assert isinstance(task, CrawlTask)
        return {"task_calls": 1, "fetched_pages": min(task.input.max_pages, policy.crawl.max_pages)}

    # ------------------------------------------------------------------ run

    def execute(self, ctx: ExecutionContext) -> EngineOutcome:
        assert isinstance(ctx.task, CrawlTask)
        inp = ctx.task.input
        pol = ctx.policy
        db = ctx.app.db
        crawl_id = ctx.task_id
        collection = documents.resolve_collection(db.read(), inp.collection_id)
        scope = crawl_scope(inp)
        max_pages = min(inp.max_pages, pol.crawl.max_pages)
        max_depth = min(inp.max_depth, pol.crawl.max_depth)
        duration_s = min(inp.max_duration_ms, pol.crawl.max_duration_ms) / 1000
        deadline = Deadline(min(duration_s, ctx.remaining_ms() / 1000), ctx.cancel)
        stats: dict[str, Any] = {
            "pages_stored": 0,
            "pages_unchanged": 0,
            "pages_skipped": 0,
            "pages_failed": 0,
            "rejected": 0,
            "requests": 0,
            "wire_bytes": 0,
            "queued": 0,
        }
        stored: list[Evidence] = []
        usage = {"task_calls": 1, "fetched_pages": 0}
        frontier: deque[tuple[str, int]] = deque()
        seen: set[str] = set()
        last_hit: dict[str, float] = {}
        stop_reason = "complete"

        def enqueue(url: str, depth: int) -> None:
            try:
                canon = canonicalize(url)
            except UrlError:
                return
            if canon in seen or host_of(canon) not in scope or len(seen) >= max_pages * 5:
                return
            seen.add(canon)
            frontier.append((canon, depth))
            stats["queued"] += 1

        def save_progress(state: str = "running") -> None:
            with db.tx() as conn:
                conn.execute(
                    "UPDATE crawls SET stats = ?, state = ? WHERE id = ?", (dumps(stats), state, crawl_id)
                )

        with Fetcher(pol.direct_web, EgressPolicy.from_config(self.config), extra_allowed_hosts=tuple(scope)) as f:

            def metered_get(url: str, cap: int, types: tuple[str, ...] | None = None) -> Any:
                """Reserve one page (two the first time a host's robots.txt must be read), fetch, settle."""
                need = 1 if f"{urlsplit(url).scheme}://{urlsplit(url).netloc}" in f._robots else 2
                assert ctx.reserve_more is not None and ctx.settle_more is not None
                handle = ctx.reserve_more({"fetched_pages": need})
                before = f.stats.requests, f.stats.wire_bytes
                try:
                    return f.fetch(url, deadline, max_bytes=cap, allowed_types=types)
                finally:
                    pages = f.stats.requests - before[0]
                    stats["requests"] += pages
                    stats["wire_bytes"] += f.stats.wire_bytes - before[1]
                    usage["fetched_pages"] += pages
                    ctx.settle_more(handle, {"fetched_pages": pages})

            # Seeds from sitemaps (and sitemap indexes, one level deep).
            for sm in inp.sitemaps[:MAX_SITEMAP_CHILDREN]:
                try:
                    res = metered_get(sm, SITEMAP_MAX_BYTES, ("application/xml", "text/xml", "text/plain"))
                except GatewayError as exc:
                    if exc.code == ErrorCode.BUDGET_EXHAUSTED:
                        stop_reason = "budget_exhausted"
                        break
                    raise
                except FetchFailure:
                    stats["pages_failed"] += 1
                    continue
                urls, children = _parse_sitemap(res.text)
                for child in children[:MAX_SITEMAP_CHILDREN]:
                    try:
                        sub = metered_get(child, SITEMAP_MAX_BYTES, ("application/xml", "text/xml", "text/plain"))
                        urls.extend(_parse_sitemap(sub.text)[0])
                    except (FetchFailure, GatewayError):
                        stats["pages_failed"] += 1
                for u in urls:
                    enqueue(u, 0)
            for s in inp.seeds:
                enqueue(s, 0)

            page_cap = pol.direct_web.max_response_bytes
            while frontier and stop_reason == "complete":
                if ctx.cancel.cancelled:
                    stop_reason = "canceled"
                    break
                if deadline.remaining() <= 0:
                    stop_reason = "duration"
                    break
                if stats["pages_stored"] + stats["pages_unchanged"] >= max_pages:
                    stop_reason = "max_pages"
                    break
                if stats["wire_bytes"] >= pol.crawl.max_total_bytes:
                    stop_reason = "max_total_bytes"
                    break
                url, depth = frontier.popleft()
                host = host_of(url)
                delay = max(pol.crawl.min_host_delay_ms / 1000, f.robots_delays.get(urlsplit(url).netloc, 0.0))
                wait = last_hit.get(host, 0.0) + min(delay, MAX_HOST_BACKOFF_S) - time.monotonic()
                if wait > 0 and ctx.cancel.wait(min(wait, deadline.remaining())):
                    stop_reason = "canceled"
                    break
                last_hit[host] = time.monotonic()
                try:
                    res = metered_get(url, page_cap)
                except GatewayError as exc:
                    if exc.code == ErrorCode.BUDGET_EXHAUSTED:
                        stop_reason = "budget_exhausted"
                        break
                    raise
                except FetchFailure as exc:
                    if exc.code in (ErrorCode.CANCELED, ErrorCode.DEADLINE_EXCEEDED) and (
                        ctx.cancel.cancelled or deadline.remaining() <= 0
                    ):
                        stop_reason = "canceled" if ctx.cancel.cancelled else "duration"
                        break
                    stats["rejected" if exc.blocked else "pages_failed"] += 1
                    continue
                name = "page.html" if res.content_type in ("text/html", "application/xhtml+xml") else "page.txt"
                try:
                    ex = extract(
                        name, res.text.encode("utf-8"), declared_media_type=res.content_type,
                        original_url=res.final_url, max_chars=documents.MAX_DOCUMENT_CHARS,
                    )
                except RejectedImport:
                    stats["pages_skipped"] += 1
                    continue
                with db.tx() as conn:
                    result = documents.ingest(
                        conn,
                        collection["id"],
                        local_identifier=res.final_url,
                        source_type="crawled_page",
                        extracted=ex,
                        original_url=url,
                        now=datetime.now(UTC),
                        fetched_at=res.fetched_at,
                    )
                stats["pages_unchanged" if result["status"] == "unchanged" else "pages_stored"] += 1
                if len(stored) < 50:
                    stored.append(
                        Evidence(
                            source_id=result["document_id"],
                            source_type="crawled_page",
                            version_digest=result["version_digest"],
                            excerpt=ex.text[: ctx.max_excerpt()],
                            title=ex.title,
                            local_identifier=res.final_url,
                            original_url=url,
                            canonical_url=ex.canonical_url,
                            document_id=result["document_id"],
                            collection_id=collection["id"],
                            fetched_at=res.fetched_at.isoformat(),
                            extraction_method=ex.method,
                            content_chars=len(ex.text),
                        )
                    )
                if not inp.exact_urls_only and depth < max_depth and name == "page.html":
                    for link in _links(res.text, res.final_url)[:MAX_LINKS_PER_PAGE]:
                        enqueue(link, depth + 1)
                if (stats["pages_stored"] + stats["pages_unchanged"]) % 5 == 0:
                    save_progress()

        kept = stats["pages_stored"] + stats["pages_unchanged"]
        with db.tx() as conn:
            conn.execute(
                "UPDATE crawls SET stats = ?, state = ?, stop_reason = ?, finished_at = ? WHERE id = ?",
                (dumps(stats), "finished", stop_reason, iso(datetime.now(UTC)), crawl_id),
            )
        outcome = EngineOutcome(
            ok=True,
            evidence=stored,
            usage=usage,
            coverage={
                "kind": "live_destination",
                "collection": collection["name"],
                "scope_hosts": sorted(scope),
                "pages_kept": kept,
                "stop_reason": stop_reason,
                **stats,
                "statement": f"Crawled {kept} page(s) into the collection '{collection['name']}' "
                f"(stopped: {stop_reason}). Search them with scope 'owned_corpus'.",
            },
            content=None,
            partial=stop_reason not in ("complete", "max_pages"),
        )
        if stop_reason == "canceled":
            outcome.ok = False
            outcome.error = GatewayError(ErrorCode.CANCELED, "the crawl was cancelled; pages already stored are kept")
        elif stop_reason == "budget_exhausted" and kept == 0:
            outcome.ok = False
            outcome.error = GatewayError(ErrorCode.BUDGET_EXHAUSTED, "a limit was reached before any page was stored")
        return outcome
