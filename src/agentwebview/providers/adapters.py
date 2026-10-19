"""The connected providers: Exa (search), Tavily (search), Firecrawl (read page, search), Jina Reader
(read page), Parallel, Linkup, SerpApi and You.com (search).

Request and response shapes, prices and error codes follow each provider's official documentation as read
in October 2026 (links in each spec). Where a provider reports what a call cost, settlement uses that; where
it does not (Firecrawl scrape, Jina, Parallel, Linkup, You.com), settlement uses the price-table estimate the
reservation was made with. Prices and free tiers change: check the provider's pricing page.
"""

from __future__ import annotations

import hashlib
import math
from datetime import UTC, datetime
from typing import Any

from agentwebview.contract import AnyTask
from agentwebview.engine import EngineOutcome, Evidence, ExecutionContext
from agentwebview.errors import ErrorCode, GatewayError
from agentwebview.policy import Policy
from agentwebview.providers.base import (
    MAX_CONTENT_CHARS,
    BalanceReading,
    ProviderEngine,
    ProviderSpec,
    now_iso,
    read_input,
    search_input,
    usd_to_micro,
    web_evidence,
)
from agentwebview.providers.http import ProviderHTTP, ProviderResponse
from agentwebview.urls import UrlError, canonicalize, host_of


class KeyCheckFailed(Exception):
    def __init__(self, resp: ProviderResponse) -> None:
        super().__init__(f"HTTP {resp.status}")
        self.resp = resp


def _obj(resp: ProviderResponse) -> dict[str, Any]:
    """The JSON object body (empty when the body is not an object)."""
    return resp.body if isinstance(resp.body, dict) else {}


def _sub(obj: Any, key: str) -> dict[str, Any]:
    """A nested JSON object (empty when missing or not an object)."""
    value = obj.get(key) if isinstance(obj, dict) else None
    return value if isinstance(value, dict) else {}


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _date(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _search_outcome(
    engine: ProviderEngine,
    ctx: ExecutionContext,
    evidence: list[Evidence],
    units: int | None,
    *,
    reported: bool,
    extra: dict[str, Any] | None = None,
) -> EngineOutcome:
    name = engine.spec.name
    return EngineOutcome(
        ok=True,
        evidence=evidence,
        usage=engine.usage(ctx, units),
        coverage={
            "kind": "web_index",
            "provider": engine.spec.provider_id,
            "engine": engine.descriptor.id,
            "results": len(evidence),
            "statement": f"Results come from {name}'s web index through your connected {name} key. Its "
            "coverage, ranking and freshness are the provider's; this is not a complete or global web search.",
            **(extra or {}),
        },
        detail={"provider": engine.spec.provider_id, "cost_reported": reported},
    )


def _read_outcome(
    engine: ProviderEngine,
    ctx: ExecutionContext,
    url: str,
    final_url: str,
    title: str | None,
    text: str,
    published: str | None,
    units: int | None,
    *,
    reported: bool,
    status: int | None = None,
) -> EngineOutcome:
    body = text or ""
    content = body[:MAX_CONTENT_CHARS]
    try:
        canon = canonicalize(final_url or url)
    except UrlError:
        canon = url
    ev = Evidence(
        source_id="url:" + hashlib.sha256(canon.encode()).hexdigest()[:24],
        source_type="live_page",
        version_digest=hashlib.sha256(body.encode()).hexdigest(),
        excerpt=" ".join(content.split())[: engine.max_excerpt(ctx)],
        title=title or None,
        original_url=url,
        canonical_url=canon,
        fetched_at=now_iso(),
        published_at=published or None,
        extraction_method=f"{engine.spec.provider_id}:read",
        truncated=len(body) > MAX_CONTENT_CHARS,
        content_chars=len(body),
    )
    name = engine.spec.name
    try:
        host = host_of(canon)
    except UrlError:
        host = ""
    return EngineOutcome(
        ok=True,
        evidence=[ev] if body.strip() else [],
        usage=engine.usage(ctx, units),
        coverage={
            "kind": "live_destination",
            "url": final_url or url,
            "host": host,
            "provider": engine.spec.provider_id,
            "http_status": status,
            "statement": f"Content was fetched from the requested destination by {name} through your connected "
            f"{name} key (AgentWebView made no request to the destination itself).",
        },
        content=content,
        partial=ev.truncated,
        detail={"provider": engine.spec.provider_id, "cost_reported": reported},
    )


# =========================================================================== Exa

EXA = ProviderSpec(
    provider_id="exa",
    name="Exa",
    hosts=frozenset({"api.exa.ai"}),
    unit="exa_usd_micro",
    unit_label="USD micro-units (1 USD = 1,000,000)",
    pricing_url="https://exa.ai/pricing",
    docs_url="https://exa.ai/docs/reference/search",
    signup_url="https://exa.ai/pricing",
    terms_url="https://exa.ai/assets/Exa_Labs_Terms_of_Service.pdf",
    free_key_check=False,
    free_allowance=(10_000_000, "monthly"),
    free_tier_text="$10 of free credit a month",
    notes="Settles on the costDollars.total each response reports. Exa has no balance endpoint for an ordinary "
    "API key, so the key is verified by its first successful call.",
)
EXA_SEARCH_USD_MICRO = 7_000  # auto search, up to 10 results ($7 / 1k requests)
EXA_EXTRA_RESULT_USD_MICRO = 1_000  # each result above 10 ($1 / 1k)
EXA_TEXT_PER_RESULT_USD_MICRO = 1_000  # contents text ($1 / 1k pages); charged conservatively when requested


class ExaSearch(ProviderEngine):
    spec = EXA
    engine_id = "exa"
    operations = frozenset({"search"})
    coverage = frozenset({"web_index"})
    capabilities = frozenset({"web_search", "neural", "contents", "excerpts"})
    default_latency_ms = 1500

    def describe(self) -> str:
        return "Exa web search through your Exa key. Neural web index; results include page text excerpts."

    def _n(self, task: AnyTask, policy: Policy) -> int:
        return max(1, min(task.limits.max_results, policy.max_results, 100))

    def estimate_units(self, task: AnyTask, policy: Policy) -> int:
        n = self._n(task, policy)
        return EXA_SEARCH_USD_MICRO + max(0, n - 10) * EXA_EXTRA_RESULT_USD_MICRO + n * EXA_TEXT_PER_RESULT_USD_MICRO

    def call(self, http: ProviderHTTP, key: str, ctx: ExecutionContext) -> EngineOutcome:
        inp = search_input(ctx)
        n = self._n(ctx.task, ctx.policy)
        resp = http.request(
            "POST",
            "https://api.exa.ai/search",
            headers={"x-api-key": key, "Content-Type": "application/json"},
            json_body={
                "query": inp.query,
                "numResults": n,
                "type": "auto",
                "contents": {"text": {"maxCharacters": max(200, self.max_excerpt(ctx))}},
            },
            timeout_s=self.timeout_s(ctx),
        )
        if resp.status != 200 or not isinstance(resp.body, dict):
            return self.failure(ctx, resp)
        evidence: list[Evidence] = []
        for i, r in enumerate(_obj(resp).get("results") or []):
            if not isinstance(r, dict) or not r.get("url"):
                continue
            text = r.get("text") or " ".join(r.get("highlights") or []) or r.get("summary") or ""
            ev = web_evidence(
                provider="exa",
                url=str(r["url"]),
                title=r.get("title"),
                text=str(text),
                max_chars=self.max_excerpt(ctx),
                rank=round(1.0 / (i + 1), 6),
                published=r.get("publishedDate"),
                method="search",
            )
            if ev is not None:
                evidence.append(ev)
        cost = _obj(resp).get("costDollars")
        units = usd_to_micro(cost.get("total")) if isinstance(cost, dict) else None
        return _search_outcome(
            self, ctx, evidence[:n], units, reported=units is not None, extra={"request_id": _obj(resp).get("requestId")}
        )


# =========================================================================== Tavily

TAVILY = ProviderSpec(
    provider_id="tavily",
    name="Tavily",
    hosts=frozenset({"api.tavily.com"}),
    unit="tavily_credits",
    unit_label="Tavily API credits",
    pricing_url="https://www.tavily.com/pricing",
    docs_url="https://docs.tavily.com/documentation/api-reference/endpoint/search",
    signup_url="https://www.tavily.com/pricing",
    terms_url="https://www.tavily.com/terms",
    free_key_check=True,
    free_allowance=(1_000, "monthly"),
    free_tier_text="1,000 credits a month",
    notes="Basic search costs 1 credit (reported in usage.credits). GET /usage reports key and plan usage; it is "
    "limited to 10 requests per 10 minutes.",
)


class TavilySearch(ProviderEngine):
    spec = TAVILY
    engine_id = "tavily"
    operations = frozenset({"search"})
    coverage = frozenset({"web_index"})
    capabilities = frozenset({"web_search", "excerpts", "relevance_scores"})
    default_latency_ms = 1200

    def describe(self) -> str:
        return "Tavily web search (basic depth) through your Tavily key."

    def estimate_units(self, task: AnyTask, policy: Policy) -> int:
        return 1

    def credits_exhausted(self, resp: ProviderResponse) -> bool:
        return resp.status in (402, 432, 433)

    def call(self, http: ProviderHTTP, key: str, ctx: ExecutionContext) -> EngineOutcome:
        inp = search_input(ctx)
        n = max(1, min(self.max_results(ctx), 20))
        resp = http.request(
            "POST",
            "https://api.tavily.com/search",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json_body={"query": inp.query, "max_results": n, "search_depth": "basic", "include_usage": True},
            timeout_s=self.timeout_s(ctx),
        )
        if resp.status != 200 or not isinstance(resp.body, dict):
            return self.failure(ctx, resp)
        evidence: list[Evidence] = []
        for r in _obj(resp).get("results") or []:
            if not isinstance(r, dict) or not r.get("url"):
                continue
            score = r.get("score")
            ev = web_evidence(
                provider="tavily",
                url=str(r["url"]),
                title=r.get("title"),
                text=str(r.get("content") or ""),
                max_chars=self.max_excerpt(ctx),
                rank=round(float(score), 6) if isinstance(score, int | float) else None,
                published=r.get("published_date"),
                method="search",
            )
            if ev is not None:
                evidence.append(ev)
        usage = _obj(resp).get("usage")
        units = _int(usage.get("credits")) if isinstance(usage, dict) else None
        return _search_outcome(
            self, ctx, evidence[:n], units, reported=units is not None, extra={"request_id": _obj(resp).get("request_id")}
        )

    def check_key(self, http: ProviderHTTP, key: str) -> BalanceReading | None:
        resp = http.request("GET", "https://api.tavily.com/usage", headers={"Authorization": f"Bearer {key}"})
        if resp.status != 200 or not isinstance(resp.body, dict):
            raise KeyCheckFailed(resp)
        k = _sub(_obj(resp), "key")
        a = _sub(_obj(resp), "account")
        used, limit = _int(k.get("usage")), _int(k.get("limit"))
        if limit is None:
            used, limit = _int(a.get("plan_usage")), _int(a.get("plan_limit"))
        remaining = None if limit is None or used is None else max(0, limit - used)
        return BalanceReading(remaining=remaining, used=used, allowance=limit, raw={"key": k, "account": a})


# =========================================================================== Firecrawl

FIRECRAWL = ProviderSpec(
    provider_id="firecrawl",
    name="Firecrawl",
    hosts=frozenset({"api.firecrawl.dev"}),
    unit="firecrawl_credits",
    unit_label="Firecrawl credits",
    pricing_url="https://www.firecrawl.dev/pricing",
    docs_url="https://docs.firecrawl.dev/api-reference/endpoint/scrape",
    signup_url="https://www.firecrawl.dev/pricing",
    terms_url="https://www.firecrawl.dev/terms-of-service",
    operations=("search", "read_page"),
    free_key_check=True,
    free_allowance=(1_000, "monthly"),
    free_tier_text="1,000 credits a month",
    notes="Scrape costs 1 credit per page (not reported in the response); search costs 2 credits per 10 results "
    "(reported as creditsUsed). GET /v2/team/credit-usage reports remaining credits for the billing period.",
)


class FirecrawlEngine(ProviderEngine):
    spec = FIRECRAWL
    engine_id = "firecrawl"
    operations = frozenset({"read_page", "search"})
    coverage = frozenset({"live_destination", "web_index"})
    capabilities = frozenset({"scrape", "markdown", "javascript_rendering", "web_search", "extraction"})
    default_latency_ms = 2500

    def describe(self) -> str:
        return (
            "Firecrawl scrape (one page to markdown, JavaScript rendered by Firecrawl) and web search through "
            "your Firecrawl key."
        )

    def estimate_units(self, task: AnyTask, policy: Policy) -> int:
        if task.operation == "search":
            n = max(1, min(task.limits.max_results, policy.max_results, 100))
            return 2 * math.ceil(n / 10)
        return 1

    def call(self, http: ProviderHTTP, key: str, ctx: ExecutionContext) -> EngineOutcome:
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        if ctx.task.operation == "search":
            inp = search_input(ctx)
            n = max(1, min(self.max_results(ctx), 100))
            resp = http.request(
                "POST",
                "https://api.firecrawl.dev/v2/search",
                headers=headers,
                json_body={"query": inp.query, "limit": n},
                timeout_s=self.timeout_s(ctx),
            )
            if resp.status != 200 or not isinstance(resp.body, dict) or _obj(resp).get("success") is False:
                return self.failure(ctx, resp) if resp.status != 200 else self._refused(ctx, resp)
            data = _obj(resp).get("data")
            web = data.get("web") if isinstance(data, dict) else data if isinstance(data, list) else []
            evidence: list[Evidence] = []
            for i, r in enumerate(web or []):
                if not isinstance(r, dict) or not r.get("url"):
                    continue
                ev = web_evidence(
                    provider="firecrawl",
                    url=str(r["url"]),
                    title=r.get("title"),
                    text=str(r.get("markdown") or r.get("description") or ""),
                    max_chars=self.max_excerpt(ctx),
                    rank=round(1.0 / (i + 1), 6),
                    published=None,
                    method="search",
                )
                if ev is not None:
                    evidence.append(ev)
            units = _int(_obj(resp).get("creditsUsed"))
            return _search_outcome(self, ctx, evidence[:n], units, reported=units is not None)
        url = read_input(ctx).url or ""
        resp = http.request(
            "POST",
            "https://api.firecrawl.dev/v2/scrape",
            headers=headers,
            json_body={
                "url": url,
                "formats": ["markdown"],
                "onlyMainContent": True,
                "timeout": int(max(1, self.timeout_s(ctx) - 1) * 1000),
            },
            timeout_s=self.timeout_s(ctx),
        )
        if resp.status != 200 or not isinstance(resp.body, dict):
            return self.failure(ctx, resp)
        if _obj(resp).get("success") is False:
            return self._refused(ctx, resp)
        data = _sub(_obj(resp), "data")
        meta = _sub(data, "metadata")
        final = str(meta.get("url") or meta.get("sourceURL") or url)
        return _read_outcome(
            self,
            ctx,
            url,
            final,
            meta.get("title"),
            str(data.get("markdown") or ""),
            None,
            1,  # 1 credit per scraped page, including 403/404 pages
            reported=False,
            status=_int(meta.get("statusCode")),
        )

    def _refused(self, ctx: ExecutionContext, resp: ProviderResponse) -> EngineOutcome:
        return self._fail(
            ctx,
            GatewayError(ErrorCode.ENGINE_FAILURE, f"Firecrawl could not complete the request ({resp.text[:200]})"),
            units=0,
        )

    def check_key(self, http: ProviderHTTP, key: str) -> BalanceReading | None:
        resp = http.request(
            "GET", "https://api.firecrawl.dev/v2/team/credit-usage", headers={"Authorization": f"Bearer {key}"}
        )
        if resp.status != 200 or not isinstance(resp.body, dict):
            raise KeyCheckFailed(resp)
        d = _sub(_obj(resp), "data")
        remaining, plan = _int(d.get("remainingCredits")), _int(d.get("planCredits"))
        return BalanceReading(
            remaining=remaining,
            used=None if remaining is None or plan is None else max(0, plan - remaining),
            allowance=plan,
            period_start=_date(d.get("billingPeriodStart")),
            period_end=_date(d.get("billingPeriodEnd")),
            raw=d,
        )


# =========================================================================== Jina Reader

JINA = ProviderSpec(
    provider_id="jina_reader",
    name="Jina Reader",
    hosts=frozenset({"r.jina.ai", "eu.r.jina.ai"}),
    unit="jina_tokens",
    unit_label="Jina output tokens",
    pricing_url="https://jina.ai/reader/",
    docs_url="https://r.jina.ai/docs",
    signup_url="https://jina.ai/reader/",
    terms_url="https://jina.ai/legal/",
    operations=("read_page",),
    free_key_check=False,
    free_allowance=None,
    free_tier_text="10M tokens for non-commercial use (enter your allowance)",
    notes="Billed on output tokens; failed requests are not charged. The free tokens are for non-commercial use "
    "only. Each read sends X-Token-Budget equal to the reservation, so a page can never cost more than was "
    "reserved. No balance endpoint is documented.",
)
JINA_PAGE_TOKEN_BUDGET = 60_000  # reservation (and X-Token-Budget) per page read


class JinaReader(ProviderEngine):
    spec = JINA
    engine_id = "jina_reader"
    operations = frozenset({"read_page"})
    coverage = frozenset({"live_destination"})
    capabilities = frozenset({"extraction", "markdown", "javascript_rendering"})
    default_latency_ms = 2000

    def describe(self) -> str:
        return "Jina Reader: one page to clean text/markdown through your Jina key."

    def estimate_units(self, task: AnyTask, policy: Policy) -> int:
        return JINA_PAGE_TOKEN_BUDGET

    def call(self, http: ProviderHTTP, key: str, ctx: ExecutionContext) -> EngineOutcome:
        url = read_input(ctx).url or ""
        budget = int(ctx.reserved.get(self.spec.unit, JINA_PAGE_TOKEN_BUDGET))
        resp = http.request(
            "POST",
            "https://r.jina.ai/",
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
                "X-Respond-With": "markdown",
                "X-Token-Budget": str(budget),
                "X-Timeout": str(int(min(180, max(1, self.timeout_s(ctx) - 1)))),
            },
            json_body={"url": url},
            timeout_s=self.timeout_s(ctx),
        )
        if resp.status == 409:  # the page would cost more than the reserved token budget: refused, not charged
            return self._fail(
                ctx,
                GatewayError(ErrorCode.ENGINE_FAILURE, f"Jina Reader: the page exceeds the reserved budget of {budget} tokens"),
                units=0,
            )
        if resp.status != 200 or not isinstance(resp.body, dict):
            return self.failure(ctx, resp)
        data = _sub(_obj(resp), "data")
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else _obj(resp).get("usage")
        tokens = _int(usage.get("tokens")) if isinstance(usage, dict) else None
        content = str(data.get("content") or data.get("text") or "")
        if tokens is None:
            tokens = min(budget, max(1, math.ceil(len(content) / 4)))  # estimate: ~4 characters per token
        return _read_outcome(
            self,
            ctx,
            url,
            str(data.get("url") or url),
            data.get("title"),
            content,
            data.get("publishedTime"),
            min(tokens, budget),
            reported=isinstance(usage, dict),
            status=_int(data.get("httpStatus")),
        )


# =========================================================================== Parallel

PARALLEL = ProviderSpec(
    provider_id="parallel",
    name="Parallel",
    hosts=frozenset({"api.parallel.ai"}),
    unit="parallel_usd_micro",
    unit_label="USD micro-units (1 USD = 1,000,000)",
    pricing_url="https://parallel.ai/pricing",
    docs_url="https://docs.parallel.ai/api-reference/search/search",
    signup_url="https://parallel.ai/pricing",
    free_key_check=False,
    free_allowance=(5_000_000, "monthly"),
    free_tier_text="$5 of free credit a month (needs a card on file at Parallel)",
    notes="Search API in 'fast' mode: $1 per 1,000 requests, plus $1 per 1,000 results beyond 10 (settled on that "
    "estimate). Out of credit answers HTTP 402. The key is verified by its first successful call. Parallel's own "
    "spending limits only send email: the local allowance here is the hard stop.",
)
PARALLEL_SEARCH_USD_MICRO = 1_000  # 'fast' mode, up to 10 results ($1 / 1k requests)
PARALLEL_EXTRA_RESULT_USD_MICRO = 1_000  # each result above 10 ($1 / 1k)


class ParallelSearch(ProviderEngine):
    spec = PARALLEL
    engine_id = "parallel"
    operations = frozenset({"search"})
    coverage = frozenset({"web_index"})
    capabilities = frozenset({"web_search", "excerpts"})
    default_latency_ms = 1500

    def describe(self) -> str:
        return "Parallel web search (fast mode) through your Parallel key."

    def _n(self, task: AnyTask, policy: Policy) -> int:
        return max(1, min(task.limits.max_results, policy.max_results, 20))

    def estimate_units(self, task: AnyTask, policy: Policy) -> int:
        return PARALLEL_SEARCH_USD_MICRO + max(0, self._n(task, policy) - 10) * PARALLEL_EXTRA_RESULT_USD_MICRO

    def call(self, http: ProviderHTTP, key: str, ctx: ExecutionContext) -> EngineOutcome:
        inp = search_input(ctx)
        n = self._n(ctx.task, ctx.policy)
        resp = http.request(
            "POST",
            "https://api.parallel.ai/v1/search",
            headers={"x-api-key": key, "Content-Type": "application/json"},
            json_body={
                "objective": inp.query,
                "search_queries": [inp.query],
                "mode": "fast",
                "advanced_settings": {
                    "max_results": n,
                    "excerpt_settings": {"max_chars_per_result": max(200, self.max_excerpt(ctx))},
                },
            },
            timeout_s=self.timeout_s(ctx),
        )
        if resp.status != 200 or not isinstance(resp.body, dict):
            return self.failure(ctx, resp)
        evidence: list[Evidence] = []
        for i, r in enumerate(_obj(resp).get("results") or []):
            if not isinstance(r, dict) or not r.get("url"):
                continue
            excerpts = r.get("excerpts")
            text = " ".join(str(x) for x in excerpts) if isinstance(excerpts, list) else ""
            ev = web_evidence(
                provider="parallel",
                url=str(r["url"]),
                title=r.get("title"),
                text=text,
                max_chars=self.max_excerpt(ctx),
                rank=round(1.0 / (i + 1), 6),
                published=r.get("publish_date"),
                method="search",
            )
            if ev is not None:
                evidence.append(ev)
        return _search_outcome(
            self, ctx, evidence[:n], None, reported=False, extra={"request_id": _obj(resp).get("search_id")}
        )


# =========================================================================== Linkup

LINKUP = ProviderSpec(
    provider_id="linkup",
    name="Linkup",
    hosts=frozenset({"api.linkup.so"}),
    unit="linkup_usd_micro",
    unit_label="USD micro-units (1 USD = 1,000,000)",
    pricing_url="https://www.linkup.so/pricing",
    docs_url="https://docs.linkup.so/pages/documentation/endpoints/search/reference",
    signup_url="https://www.linkup.so/pricing",
    terms_url="https://www.linkup.so/terms-of-use",
    free_key_check=False,
    free_allowance=(20_000_000, "monthly"),
    free_tier_text="Eligible accounts are topped back up to $20 a month",
    notes="Standard-depth search returning search results: $0.005 per call (settled on that price; responses report "
    "no cost). Linkup answers HTTP 429 both when credit runs out and for too many concurrent requests; the error code "
    "in the body tells them apart. The key is verified by its first successful call.",
)
LINKUP_SEARCH_USD_MICRO = 5_000  # standard depth, outputType searchResults ($0.005)
LINKUP_OUT_OF_CREDIT_CODES = frozenset({"INSUFFICIENT_FUNDS_CREDITS", "EXCEED_BUDGET_LIMIT"})


class LinkupSearch(ProviderEngine):
    spec = LINKUP
    engine_id = "linkup"
    operations = frozenset({"search"})
    coverage = frozenset({"web_index"})
    capabilities = frozenset({"web_search", "excerpts"})
    default_latency_ms = 2000

    def describe(self) -> str:
        return "Linkup web search (standard depth) through your Linkup key."

    def estimate_units(self, task: AnyTask, policy: Policy) -> int:
        return LINKUP_SEARCH_USD_MICRO

    def credits_exhausted(self, resp: ProviderResponse) -> bool:
        if resp.status == 402:
            return True
        if resp.status != 429:
            return False
        err = _sub(_obj(resp), "error")
        if str(err.get("code") or "").upper() in LINKUP_OUT_OF_CREDIT_CODES:
            return True
        return "credit" in str(err.get("message") or "").lower()

    def call(self, http: ProviderHTTP, key: str, ctx: ExecutionContext) -> EngineOutcome:
        inp = search_input(ctx)
        n = max(1, min(self.max_results(ctx), 20))
        resp = http.request(
            "POST",
            "https://api.linkup.so/v1/search",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json_body={"q": inp.query, "depth": "standard", "outputType": "searchResults", "maxResults": n},
            timeout_s=self.timeout_s(ctx),
        )
        if resp.status != 200 or not isinstance(resp.body, dict):
            return self.failure(ctx, resp)
        evidence: list[Evidence] = []
        for i, r in enumerate(_obj(resp).get("results") or []):
            if not isinstance(r, dict) or not r.get("url") or r.get("type", "text") != "text":
                continue
            ev = web_evidence(
                provider="linkup",
                url=str(r["url"]),
                title=r.get("name"),
                text=str(r.get("content") or ""),
                max_chars=self.max_excerpt(ctx),
                rank=round(1.0 / (i + 1), 6),
                published=None,
                method="search",
            )
            if ev is not None:
                evidence.append(ev)
        return _search_outcome(self, ctx, evidence[:n], None, reported=False)


# =========================================================================== SerpApi

SERPAPI = ProviderSpec(
    provider_id="serpapi",
    name="SerpApi",
    hosts=frozenset({"serpapi.com"}),
    unit="serpapi_searches",
    unit_label="SerpApi searches",
    pricing_url="https://serpapi.com/pricing",
    docs_url="https://serpapi.com/search-api",
    signup_url="https://serpapi.com/pricing",
    free_key_check=True,
    free_allowance=(250, "monthly"),
    free_tier_text="250 searches a month",
    notes="Google results through SerpApi: one search per call (empty results count; cached and failed searches do "
    "not). The key travels as the api_key URL parameter because SerpApi accepts it nowhere else; request URLs are "
    "never logged. GET /account.json (free, not counted) checks the key and reports searches left this month.",
)
SERPAPI_OUT_OF_SEARCHES = "run out of searches"


class SerpApiSearch(ProviderEngine):
    spec = SERPAPI
    engine_id = "serpapi"
    operations = frozenset({"search"})
    coverage = frozenset({"web_index"})
    capabilities = frozenset({"web_search", "excerpts"})
    default_latency_ms = 2500

    def describe(self) -> str:
        return "Google web results through your SerpApi key."

    def estimate_units(self, task: AnyTask, policy: Policy) -> int:
        return 1

    def credits_exhausted(self, resp: ProviderResponse) -> bool:
        if resp.status == 402:
            return True
        return resp.status == 429 and SERPAPI_OUT_OF_SEARCHES in str(_obj(resp).get("error") or "").lower()

    def call(self, http: ProviderHTTP, key: str, ctx: ExecutionContext) -> EngineOutcome:
        inp = search_input(ctx)
        n = max(1, min(self.max_results(ctx), 100))
        resp = http.request(
            "GET",
            "https://serpapi.com/search.json",
            headers={},
            params={"engine": "google", "q": inp.query, "api_key": key},
            timeout_s=self.timeout_s(ctx),
        )
        if resp.status != 200 or not isinstance(resp.body, dict):
            return self.failure(ctx, resp)
        evidence: list[Evidence] = []
        for i, r in enumerate(_obj(resp).get("organic_results") or []):
            if not isinstance(r, dict) or not r.get("link"):
                continue
            position = _int(r.get("position")) or i + 1
            ev = web_evidence(
                provider="serpapi",
                url=str(r["link"]),
                title=r.get("title"),
                text=str(r.get("snippet") or ""),
                max_chars=self.max_excerpt(ctx),
                rank=round(1.0 / max(1, position), 6),
                published=None,
                method="search",
            )
            if ev is not None:
                evidence.append(ev)
        meta = _sub(_obj(resp), "search_metadata")
        return _search_outcome(self, ctx, evidence[:n], 1, reported=False, extra={"request_id": meta.get("id")})

    def check_key(self, http: ProviderHTTP, key: str) -> BalanceReading | None:
        resp = http.request("GET", "https://serpapi.com/account.json", headers={}, params={"api_key": key})
        if resp.status != 200 or not isinstance(resp.body, dict):
            raise KeyCheckFailed(resp)
        a = _obj(resp)
        fields = ("plan_searches_left", "total_searches_left", "searches_per_month", "this_month_usage")
        return BalanceReading(
            remaining=_int(a.get("plan_searches_left")),
            used=_int(a.get("this_month_usage")),
            allowance=_int(a.get("searches_per_month")),
            raw={k: a.get(k) for k in fields},
        )


# =========================================================================== You.com

YOUCOM = ProviderSpec(
    provider_id="youcom",
    name="You.com",
    hosts=frozenset({"ydc-index.io"}),
    unit="youcom_usd_micro",
    unit_label="USD micro-units (1 USD = 1,000,000)",
    pricing_url="https://you.com/docs/administration/billing",
    docs_url="https://you.com/docs/api-reference/search",
    signup_url="https://you.com/docs/administration/billing",
    free_key_check=False,
    free_allowance=(10_000_000, "lifetime"),
    free_tier_text="A one-time $10 starting balance",
    notes="Web Search API: $5 per 1,000 calls (settled on that price; responses report no cost), drawn from a "
    "prepaid balance. Out of credit answers HTTP 402. The key is verified by its first successful call. Its terms "
    "limit automated use: read them before connecting a key.",
)
YOUCOM_SEARCH_USD_MICRO = 5_000  # $5 / 1k calls


class YouComSearch(ProviderEngine):
    spec = YOUCOM
    engine_id = "youcom"
    operations = frozenset({"search"})
    coverage = frozenset({"web_index"})
    capabilities = frozenset({"web_search", "excerpts"})
    default_latency_ms = 1500

    def describe(self) -> str:
        return "You.com web search through your You.com API key."

    def estimate_units(self, task: AnyTask, policy: Policy) -> int:
        return YOUCOM_SEARCH_USD_MICRO

    def call(self, http: ProviderHTTP, key: str, ctx: ExecutionContext) -> EngineOutcome:
        inp = search_input(ctx)
        n = max(1, min(self.max_results(ctx), 20))
        resp = http.request(
            "POST",
            "https://ydc-index.io/v1/search",
            headers={"X-API-Key": key, "Content-Type": "application/json"},
            json_body={"query": inp.query, "count": n},
            timeout_s=self.timeout_s(ctx),
        )
        if resp.status != 200 or not isinstance(resp.body, dict):
            return self.failure(ctx, resp)
        results = _sub(_obj(resp), "results")
        rows = [r for section in ("web", "news") for r in (results.get(section) or []) if isinstance(r, dict)]
        evidence: list[Evidence] = []
        for i, r in enumerate(rows):
            if not r.get("url"):
                continue
            snippets = r.get("snippets")
            text = " ".join(str(x) for x in snippets) if isinstance(snippets, list) and snippets else ""
            ev = web_evidence(
                provider="youcom",
                url=str(r["url"]),
                title=r.get("title"),
                text=text or str(r.get("description") or ""),
                max_chars=self.max_excerpt(ctx),
                rank=round(1.0 / (i + 1), 6),
                published=r.get("page_age"),
                method="search",
            )
            if ev is not None:
                evidence.append(ev)
        meta = _sub(_obj(resp), "metadata")
        return _search_outcome(
            self, ctx, evidence[:n], None, reported=False, extra={"request_id": meta.get("search_uuid")}
        )


ENGINE_CLASSES: tuple[type[ProviderEngine], ...] = (
    ExaSearch,
    TavilySearch,
    FirecrawlEngine,
    JinaReader,
    ParallelSearch,
    LinkupSearch,
    SerpApiSearch,
    YouComSearch,
)
SPECS: dict[str, ProviderSpec] = {c.spec.provider_id: c.spec for c in ENGINE_CLASSES}
ENGINE_FOR_PROVIDER: dict[str, str] = {c.spec.provider_id: c.engine_id for c in ENGINE_CLASSES}
PROVIDER_UNITS: frozenset[str] = frozenset(s.unit for s in SPECS.values())
