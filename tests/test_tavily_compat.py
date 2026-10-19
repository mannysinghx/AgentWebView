"""The Tavily-compatible endpoints, called by the real tavily-python client with only its base URL changed."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from conftest import UI, bearer, fake, serpapi_ok
from fastapi.testclient import TestClient

from agentwebview.service import Service

tavily = pytest.importorskip("tavily")


def _agent(service: Service, **kwargs: Any) -> str:
    _agent, token = service.create_agent("tavily code", kwargs.get("abilities"), kwargs.get("monthly"), kwargs.get("daily"))
    return token


def _client(base: str, token: str | None) -> Any:
    return tavily.TavilyClient(api_key=token, api_base_url=f"{base}/compat/tavily")


def _serpapi_many(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/account.json":
        return httpx.Response(200, json={"plan_searches_left": 250, "searches_per_month": 250, "this_month_usage": 0})
    q = request.url.params.get("q")
    rows = [
        {"position": 1, "link": "https://docs.example.com/a", "title": f"{q} guide", "snippet": f"How {q} works"},
        {"position": 2, "link": "https://blog.other.org/b", "title": "Other", "snippet": "something else"},
        {"position": 3, "link": "https://example.com/c", "title": "Example home", "snippet": f"{q} overview"},
    ]
    return httpx.Response(200, json={"organic_results": rows, "search_metadata": {"id": "s1"}})


def test_search_through_the_tavily_sdk(service: Service, live_server: str) -> None:
    serp = fake("serpapi", _serpapi_many)
    service.add_key("serpapi", "serp-key-123456")
    token = _agent(service)
    res = _client(live_server, token).search(
        "model context protocol", max_results=2, include_answer=True, include_domains=["example.com"]
    )
    assert [r["url"] for r in res["results"]] == ["https://docs.example.com/a", "https://example.com/c"]
    first = res["results"][0]
    assert first["title"] == "model context protocol guide" and first["content"] == "How model context protocol works"
    assert isinstance(first["score"], float) and first["raw_content"] is None
    assert res["answer"] is None and res["images"] == [] and res["query"] == "model context protocol"
    meta = res["agentwebview"]
    assert meta["provider"] == "serpapi" and meta["engine"] == "serpapi"
    assert meta["ignored_parameters"] == ["include_answer"]
    assert meta["resource_usage"]["serpapi_searches"] == "1"
    assert res["request_id"] == meta["task_id"]
    assert len([c for c in serp.calls if c.url.path == "/search.json"]) == 1
    # the call was recorded like any other agent call
    receipt = service.get_task(meta["task_id"])
    assert receipt["status"] == "succeeded"
    assert service.activity(limit=1)[0]["origin"] == "tavily"


def test_extract_through_the_tavily_sdk(service: Service, live_server: str, site: dict[str, Any]) -> None:
    paragraphs = [
        "AgentWebView reads public pages through a guarded connection and extracts the main text.",
        "Every address is resolved once and checked to be public before the connection is opened.",
        "Redirects are followed by hand, so each hop is checked again against the same rules.",
        "Bodies are capped in size, and compressed responses are refused when they expand too far.",
    ]
    site["pages"]["/robots.txt"] = (200, "text/plain", "User-agent: *\nAllow: /\n")
    site["pages"]["/page"] = (
        200,
        "text/html",
        "<html><head><title>T</title></head><body><article>" + "".join(f"<p>{p}</p>" for p in paragraphs) + "</article></body></html>",
    )
    token = _agent(service)
    res = _client(live_server, token).extract(
        ["http://site.test/page", "http://127.0.0.1:9/private"], extract_depth="advanced"
    )
    assert [r["url"] for r in res["results"]] == ["http://site.test/page"]
    assert "guarded connection" in res["results"][0]["raw_content"]
    assert [f["url"] for f in res["failed_results"]] == ["http://127.0.0.1:9/private"]
    assert res["agentwebview"]["ignored_parameters"] == ["extract_depth"]
    assert len(res["agentwebview"]["task_ids"]) == 2


def test_sdk_errors_map_to_tavily_exceptions(service: Service, live_server: str) -> None:
    from tavily.errors import BadRequestError, InvalidAPIKeyError, UsageLimitExceededError

    with pytest.raises(InvalidAPIKeyError):
        _client(live_server, "awv_not-a-real-token").search("anything")
    token = _agent(service, daily=1)
    # no provider key yet: a configuration problem, reported with what to do
    with pytest.raises(BadRequestError, match="Providers screen"):
        _client(live_server, token).search("anything")
    fake("serpapi", serpapi_ok)
    service.add_key("serpapi", "serp-key-123456")
    other = _agent(service, daily=1)
    assert _client(live_server, other).search("first")["results"]
    with pytest.raises(UsageLimitExceededError):
        _client(live_server, other).search("second")


def test_raw_http_shape_and_body_api_key(client: TestClient) -> None:
    fake("serpapi", serpapi_ok)
    assert client.post("/api/providers/serpapi/keys", json={"key": "serp-key-123456"}, headers=UI).status_code == 200
    token = client.post("/api/agents", json={"name": "raw"}, headers=UI).json()["token"]
    # older clients send the key in the body instead of the Authorization header
    res = client.post("/compat/tavily/search", json={"api_key": token, "query": "mcp", "topic": "news"})
    assert res.status_code == 200, res.text
    body = res.json()
    assert set(body) >= {"query", "answer", "images", "results", "response_time", "request_id", "follow_up_questions"}
    assert body["agentwebview"]["ignored_parameters"] == ["topic"]
    bad = client.post("/compat/tavily/search", json={"query": ""}, headers=bearer(token))
    assert bad.status_code == 400 and "query" in bad.json()["detail"]["error"]
    too_many = client.post("/compat/tavily/search", json={"query": "x", "max_results": 50}, headers=bearer(token))
    assert too_many.status_code == 400
    unauth = client.post("/compat/tavily/search", json={"query": "x"})
    assert unauth.status_code == 401 and unauth.json()["detail"]["error"]
    # the dashboard's header is not an agent credential here either
    assert client.post("/compat/tavily/search", json={"query": "x"}, headers=UI).status_code == 401
