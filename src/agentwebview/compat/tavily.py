"""Tavily-compatible ``/search`` and ``/extract``.

Code that already uses Tavily keeps working with one change, the base URL::

    TavilyClient(api_key="awv_…", api_base_url="http://127.0.0.1:8718/compat/tavily")

The key is an AgentWebView agent token (sent as ``Authorization: Bearer`` by the Tavily SDKs, or as ``api_key``
in the JSON body by older clients). Each call then goes through AgentWebView like any other: routed across all
your connected providers (free credit first, fallback when a key runs out), checked against the agent's limits,
and recorded with a receipt. Responses use Tavily's shape; the provider that actually answered, the cost and the
parameters that could not be honoured are reported under ``agentwebview`` so nothing is silently faked.

Not supported (accepted, then reported in ``agentwebview.ignored_parameters``): ``include_answer`` (an AI-written
answer needs Tavily itself; ``answer`` is null), ``include_raw_content``, ``include_images``, ``include_favicon``,
``include_usage``, ``topic``, date and time filters, ``country``, ``search_depth`` other than ``basic``,
``auto_parameters``, ``exact_match``, ``chunks_per_source`` and extract's ``extract_depth`` / ``format`` / ``query``.
``include_domains`` and ``exclude_domains`` are applied to the results after the provider answers.

Errors use Tavily's status codes and body (``{"detail": {"error": ...}}``), so the Tavily SDKs raise their usual
exceptions: 401 invalid key, 429 usage limit (limits and provider credit), 403 forbidden, 400 bad request.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from fastapi import APIRouter, Body, Request
from fastapi.responses import JSONResponse

from agentwebview.db import new_id
from agentwebview.errors import ErrorCode, GatewayError
from agentwebview.urls import UrlError, canonicalize, host_of

ORIGIN = "tavily"
MAX_RESULTS = 20
MAX_EXTRACT_URLS = 20
EXTRACT_WORKERS = 5
DEFAULT_MAX_RESULTS = 5  # Tavily's default

# The value each optional parameter has when it changes nothing; anything else is reported as ignored.
SEARCH_NEUTRAL: dict[str, tuple[Any, ...]] = {
    "search_depth": ("basic",),
    "topic": ("general",),
    "include_answer": (False,),
    "include_raw_content": (False,),
    "include_images": (False,),
    "include_image_descriptions": (False,),
    "include_favicon": (False,),
    "include_usage": (False,),
    "auto_parameters": (False,),
    "exact_match": (False,),
    "chunks_per_source": (),
    "time_range": (),
    "days": (),
    "start_date": (),
    "end_date": (),
    "max_age_hours": (),
    "country": (),
    "language": (),
    "filter_by_language": (False,),
    "fetch_timeout": (),
    "cache_fallback": (),
}
SEARCH_HANDLED = {"query", "max_results", "include_domains", "exclude_domains", "include_domains_mode", "api_key"}
EXTRACT_NEUTRAL: dict[str, tuple[Any, ...]] = {
    "include_images": (False,),
    "extract_depth": ("basic",),
    "format": ("markdown",),
    "include_favicon": (False,),
    "include_usage": (False,),
    "query": (),
    "chunks_per_source": (),
}
EXTRACT_HANDLED = {"urls", "timeout", "api_key"}

# AgentWebView error code -> the HTTP status the Tavily SDKs map to the matching exception.
STATUS = {
    ErrorCode.AUTH_REQUIRED: 401,
    ErrorCode.AUTH_SCOPE_DENIED: 403,
    ErrorCode.NETWORK_BLOCKED: 403,
    ErrorCode.BUDGET_EXHAUSTED: 429,
    ErrorCode.RATE_LIMITED: 429,
    ErrorCode.PROVIDER_CREDITS_EXHAUSTED: 429,
    ErrorCode.VALIDATION_FAILED: 400,
    ErrorCode.NO_ELIGIBLE_ENGINE: 400,
    ErrorCode.CAPABILITY_UNSUPPORTED: 400,
    ErrorCode.DEADLINE_EXCEEDED: 504,
}


def _error(status: int, message: str, *, code: str | None = None, extra: dict[str, Any] | None = None) -> JSONResponse:
    body: dict[str, Any] = {"detail": {"error": message}}
    if code or extra:
        body["agentwebview"] = {"error_code": code, **(extra or {})}
    return JSONResponse(body, status_code=status)


def _from_gateway(err: dict[str, Any] | GatewayError) -> JSONResponse:
    data = err.to_dict() if isinstance(err, GatewayError) else err
    try:
        code = ErrorCode(data.get("code", "INTERNAL"))
    except ValueError:
        code = ErrorCode.INTERNAL
    message = data.get("message") or code.value
    if data.get("remediation"):
        message = f"{message}. What to do: {data['remediation']}"
    return _error(STATUS.get(code, 502), message, code=code.value)


def _token(request: Request, body: dict[str, Any]) -> str | None:
    auth = request.headers.get("authorization") or ""
    if auth.lower().startswith("bearer ") and auth[7:].strip():
        return auth[7:].strip()
    key = body.get("api_key")
    return key if isinstance(key, str) and key else None


def _ignored(body: dict[str, Any], neutral: dict[str, tuple[Any, ...]], handled: set[str]) -> list[str]:
    out = []
    for name, value in body.items():
        if name in handled or value is None:
            continue
        if name in neutral and value in neutral[name]:
            continue
        out.append(name)
    return sorted(out)


def _domains(value: Any, name: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(d, str) for d in value):
        raise GatewayError(ErrorCode.VALIDATION_FAILED, f"{name} must be a list of domains")
    return [d.strip().lower().removeprefix("www.").rstrip(".") for d in value if d.strip()]


def _host_in(host: str, domains: list[str]) -> bool:
    host = host.lower().removeprefix("www.")
    return any(host == d or host.endswith("." + d) for d in domains)


def _host(url: str) -> str:
    try:
        return host_of(canonicalize(url))
    except UrlError:
        return ""


def _meta(result: dict[str, Any], ignored: list[str], request: Request) -> dict[str, Any]:
    coverage = result.get("coverage") or {}
    return {
        "task_id": result.get("task_id"),
        "provider": coverage.get("provider") or result.get("engine"),
        "engine": result.get("engine"),
        "resource_usage": result.get("resource_usage"),
        "cache": (result.get("cache") or {}).get("state"),
        "coverage": coverage.get("statement"),
        "ignored_parameters": ignored,
        "receipt": f"{request.base_url}#/task/{result.get('task_id')}",
    }


def router(service: Any) -> APIRouter:
    api = APIRouter(prefix="/compat/tavily", tags=["tavily-compatible"])

    def agent_for(request: Request, body: dict[str, Any], ability: str) -> dict[str, Any]:
        agent = service.authenticate(_token(request, body))
        service.require(agent, ability)
        return agent  # type: ignore[no-any-return]

    @api.post("/search", summary="Tavily-compatible search")
    def search(request: Request, body: dict[str, Any] = Body(...)) -> JSONResponse:  # noqa: B008
        started = time.monotonic()
        try:
            agent = agent_for(request, body, "search")
            query = body.get("query")
            if not isinstance(query, str) or not query.strip():
                raise GatewayError(ErrorCode.VALIDATION_FAILED, "query is required")
            raw_max = body.get("max_results", DEFAULT_MAX_RESULTS)
            if isinstance(raw_max, bool) or not isinstance(raw_max, int) or not 0 <= raw_max <= MAX_RESULTS:
                raise GatewayError(ErrorCode.VALIDATION_FAILED, f"max_results must be between 0 and {MAX_RESULTS}")
            include = _domains(body.get("include_domains"), "include_domains")
            exclude = _domains(body.get("exclude_domains"), "exclude_domains")
            prefer = body.get("include_domains_mode") == "prefer"
        except GatewayError as exc:
            return _from_gateway(exc)
        ignored = _ignored(body, SEARCH_NEUTRAL, SEARCH_HANDLED)
        if raw_max == 0:
            return JSONResponse(_search_body(query, [], started, None, ignored, request))
        # Domain filters are applied after the provider answers, so ask for more results to filter from.
        ask = min(MAX_RESULTS, raw_max * 3) if (include or exclude) else raw_max
        task = {
            "operation": "search",
            "input": {"query": query},
            "requirements": {"coverage": "web_index"},
            "limits": {"max_results": ask, "max_excerpt_chars": 1_000},
        }
        try:
            result = service.submit(task, agent=agent, origin=ORIGIN)
        except GatewayError as exc:
            return _from_gateway(exc)
        error = result.get("error") or {}
        if result.get("status") != "succeeded" and error.get("code") != "INSUFFICIENT_EVIDENCE":
            return _from_gateway(error or {"code": "INTERNAL", "message": "the search failed"})
        rows = []
        for e in result.get("evidence") or []:
            url = e.get("original_url") or e.get("canonical_url")
            if not url:
                continue
            host = _host(url)
            if exclude and _host_in(host, exclude):
                continue
            if include and not prefer and not _host_in(host, include):
                continue
            row = {
                "title": e.get("title") or url,
                "url": url,
                "content": e.get("excerpt") or "",
                "score": e.get("rank") if isinstance(e.get("rank"), int | float) else 0.0,
                "raw_content": None,
            }
            if e.get("published_at"):
                row["published_date"] = e["published_at"]
            rows.append((0 if (prefer and include and _host_in(host, include)) else 1, len(rows), row))
        rows.sort(key=lambda r: (r[0], r[1]))
        results = [r[2] for r in rows][:raw_max]
        return JSONResponse(_search_body(query, results, started, result, ignored, request))

    @api.post("/extract", summary="Tavily-compatible extract")
    def extract(request: Request, body: dict[str, Any] = Body(...)) -> JSONResponse:  # noqa: B008
        started = time.monotonic()
        try:
            agent = agent_for(request, body, "read_page")
            urls = body.get("urls")
            if isinstance(urls, str):
                urls = [urls]
            if not isinstance(urls, list) or not urls or not all(isinstance(u, str) and u.strip() for u in urls):
                raise GatewayError(ErrorCode.VALIDATION_FAILED, "urls must be a URL or a list of URLs")
            if len(urls) > MAX_EXTRACT_URLS:
                raise GatewayError(ErrorCode.VALIDATION_FAILED, f"at most {MAX_EXTRACT_URLS} URLs per request")
            timeout = body.get("timeout", 30)
            if isinstance(timeout, bool) or not isinstance(timeout, int | float) or timeout <= 0:
                raise GatewayError(ErrorCode.VALIDATION_FAILED, "timeout must be a positive number of seconds")
        except GatewayError as exc:
            return _from_gateway(exc)
        ignored = _ignored(body, EXTRACT_NEUTRAL, EXTRACT_HANDLED)
        # Finish inside the caller's own timeout (the Tavily SDK uses the same value for its HTTP timeout).
        deadline_ms = int(max(1_000, min(60_000, float(timeout) * 1000 - 2_000)))

        def read(url: str) -> dict[str, Any]:
            task = {
                "operation": "read_page",
                "input": {"url": url.strip()},
                "requirements": {"coverage": "live_destination"},
                "limits": {"deadline_ms": deadline_ms},
            }
            try:
                return service.submit(task, agent=agent, origin=ORIGIN)  # type: ignore[no-any-return]
            except GatewayError as exc:
                return {"status": "failed", "error": exc.to_dict()}

        with ThreadPoolExecutor(max_workers=min(EXTRACT_WORKERS, len(urls))) as pool:
            outcomes = list(pool.map(read, urls))
        results, failed, task_ids, usage = [], [], [], {}
        for url, out in zip(urls, outcomes, strict=True):
            if out.get("task_id"):
                task_ids.append(out["task_id"])
            for k, v in (out.get("resource_usage") or {}).items():
                usage[k] = str(int(usage.get(k, 0)) + int(v))
            if out.get("status") == "succeeded" and out.get("content"):
                results.append({"url": url, "raw_content": out["content"], "images": []})
            else:
                err = out.get("error") or {}
                failed.append({"url": url, "error": err.get("message") or "could not read this page"})
        codes = {(o.get("error") or {}).get("code") for o in outcomes if o.get("status") != "succeeded"}
        if not results and codes and codes <= {"BUDGET_EXHAUSTED", "RATE_LIMITED", "PROVIDER_CREDITS_EXHAUSTED"}:
            return _from_gateway(outcomes[0]["error"])
        return JSONResponse(
            {
                "results": results,
                "failed_results": failed,
                "response_time": round(time.monotonic() - started, 3),
                "request_id": new_id("ext_"),
                "agentwebview": {
                    "task_ids": task_ids,
                    "resource_usage": usage,
                    "ignored_parameters": ignored,
                    "note": "Each URL is one page read, counted against the agent's limits.",
                },
            }
        )

    return api


def _search_body(
    query: str,
    results: list[dict[str, Any]],
    started: float,
    result: dict[str, Any] | None,
    ignored: list[str],
    request: Request,
) -> dict[str, Any]:
    return {
        "query": query,
        "follow_up_questions": None,
        "answer": None,
        "images": [],
        "results": results,
        "response_time": round(time.monotonic() - started, 3),
        "request_id": (result or {}).get("task_id") or new_id("srch_"),
        "agentwebview": _meta(result or {}, ignored, request),
    }
