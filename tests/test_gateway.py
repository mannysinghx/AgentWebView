"""End-to-end behaviour through the real HTTP API: providers, routing, limits, receipts and security."""

from __future__ import annotations

import time
from typing import Any

import httpx
from conftest import UI, bearer, fake, make_agent, serpapi_ok, tavily_ok, web_search
from fastapi.testclient import TestClient

from agentwebview.service import Service


def add_key(client: TestClient, provider: str, key: str = "test-key-123456", **extra: Any) -> dict[str, Any]:
    res = client.post(f"/api/providers/{provider}/keys", json={"key": key, **extra}, headers=UI)
    assert res.status_code == 200, res.text
    return res.json()


# --------------------------------------------------------------------------- providers and routing


def test_web_search_uses_key_and_charges_its_allowance(client: TestClient) -> None:
    tav = fake("tavily", tavily_ok)
    key = add_key(client, "tavily")
    assert key["status"] == "ok"  # checked with the free /usage call
    assert key["allowance"]["amount"] == 1000  # the plan size the provider reported
    assert key["allowance"]["used"] == 10  # usage already reported by the provider is counted
    _agent, token = make_agent(client)
    res = client.post("/v1/tasks", json=web_search(), headers=bearer(token))
    assert res.status_code == 200, res.text
    r = res.json()
    assert r["status"] == "succeeded"
    assert r["engine"] == "tavily"
    assert r["evidence"][0]["original_url"] == "https://example.org/a"
    assert r["resource_usage"]["tavily_credits"] == "1"
    assert "not a complete or global web search" in r["coverage"]["statement"]
    after = client.get("/api/providers", headers=UI).json()["providers"]
    tavily = next(p for p in after if p["provider_id"] == "tavily")
    assert tavily["keys"][0]["allowance"]["used"] == 11
    # the provider call carried the key, the search was recorded on the receipt
    search_calls = [c for c in tav.calls if c.url.path == "/search"]
    assert len(search_calls) == 1 and search_calls[0].headers["authorization"] == "Bearer test-key-123456"
    activity = client.get("/api/activity", headers=UI).json()["items"]
    assert activity[0]["engine"] == "tavily" and activity[0]["agent_name"] == "test agent"


def test_credits_exhausted_falls_back_to_next_provider(client: TestClient) -> None:
    def tavily_out(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/usage":
            return httpx.Response(200, json={"key": {"usage": 0, "limit": 1000}})
        return httpx.Response(432, json={"detail": {"error": "This request exceeds your plan's set usage limit."}})

    fake("tavily", tavily_out)
    serp = fake("serpapi", serpapi_ok)
    add_key(client, "tavily", allowance=1000, period="monthly")
    add_key(client, "serpapi", allowance=250, period="monthly")
    # Make Tavily the first choice: it has more credit left relative to the call (score) and both are fresh.
    client.patch("/api/settings", json={"search_rotation": "score"}, headers=UI)
    r = client.post("/api/try", json=web_search("fallback test"), headers=UI).json()
    assert r["status"] == "succeeded", r
    engines = [a["engine_id"] for a in r["attempts"]]
    assert engines[-1] == "serpapi"
    if engines[0] == "tavily":  # Tavily was tried first: its credit is written off for the period
        tav = next(p for p in client.get("/api/providers", headers=UI).json()["providers"] if p["provider_id"] == "tavily")
        assert tav["keys"][0]["allowance"]["remaining"] == 0
        assert r["attempts"][0]["error_code"] == "PROVIDER_CREDITS_EXHAUSTED"
    assert len([c for c in serp.calls if c.url.path == "/search.json"]) == 1


def test_key_without_allowance_is_never_used(client: TestClient, service: Service) -> None:
    calls = fake("exa", lambda req: httpx.Response(200, json={"results": []}))
    res = client.post("/api/providers/exa/keys", json={"key": "exa-key-123456"}, headers=UI)
    key = res.json()
    client.patch(f"/api/keys/{key['id']}", json={"allowance": {"amount": None}}, headers=UI)
    r = client.post("/api/try", json=web_search(), headers=UI).json()
    assert r["status"] == "failed"
    assert r["error"]["code"] in ("BUDGET_EXHAUSTED", "NO_ELIGIBLE_ENGINE")
    assert "no allowance" in str(r["error"]).lower()
    assert calls.calls == []


def test_rejected_key_is_marked_invalid_and_skipped(client: TestClient) -> None:
    fake("serpapi", lambda req: httpx.Response(401, json={"error": "Invalid API key."}))
    key = add_key(client, "serpapi", allowance=250, period="monthly")
    assert key["status"] == "invalid"
    r = client.post("/api/try", json=web_search(), headers=UI).json()
    assert r["status"] == "failed"
    assert "rejected this key" in str(r["error"])


def test_provider_reported_cost_is_settled(client: TestClient) -> None:
    fake(
        "exa",
        lambda req: httpx.Response(
            200,
            json={
                "results": [{"url": "https://exa.example/1", "title": "Exa result", "text": "model context protocol text"}],
                "costDollars": {"total": 0.005},
                "requestId": "x",
            },
        ),
    )
    key = add_key(client, "exa")  # pre-filled with the documented $10 / month
    assert key["allowance"]["amount"] == 10_000_000
    r = client.post("/api/try", json=web_search(), headers=UI).json()
    assert r["status"] == "succeeded"
    assert r["resource_usage"]["exa_usd_micro"] == "5000"
    exa = next(p for p in client.get("/api/providers", headers=UI).json()["providers"] if p["provider_id"] == "exa")
    assert exa["keys"][0]["allowance"]["used"] == 5000


def test_cache_serves_repeat_search_without_spending_credit(client: TestClient) -> None:
    tav = fake("tavily", tavily_ok)
    add_key(client, "tavily", allowance=100, period="monthly")
    first = client.post("/api/try", json=web_search("cache me"), headers=UI).json()
    second = client.post("/api/try", json=web_search("cache me"), headers=UI).json()
    assert first["cache"]["state"] == "miss" and second["cache"]["state"] == "hit"
    assert second["resource_usage"] == {"task_calls": "1"}
    assert len([c for c in tav.calls if c.url.path == "/search"]) == 1


def test_route_preview_explains_exclusions(client: TestClient) -> None:
    fake("tavily", tavily_ok)
    add_key(client, "tavily", allowance=100, period="monthly")
    plan = client.post("/api/route/preview", json=web_search(), headers=UI).json()
    assert [c["engine_id"] for c in plan["candidates"]] == ["tavily"]
    reasons = {e["engine_id"]: e["reasons"] for e in plan["exclusions"]}
    assert any("no Exa key" in r for r in reasons["exa"])
    assert any("coverage" in r for r in reasons["owned_corpus_search"])


# --------------------------------------------------------------------------- agents and limits


def test_agent_daily_limit_is_enforced_before_the_call(client: TestClient) -> None:
    tav = fake("tavily", tavily_ok)
    add_key(client, "tavily", allowance=100, period="monthly")
    _agent, token = make_agent(client, limits={"daily": 1})
    ok = client.post("/v1/tasks", json=web_search("one"), headers=bearer(token))
    assert ok.json()["status"] == "succeeded"
    blocked = client.post("/v1/tasks", json=web_search("two"), headers=bearer(token)).json()
    assert blocked["status"] == "failed"
    assert blocked["error"]["code"] == "BUDGET_EXHAUSTED"
    assert len([c for c in tav.calls if c.url.path == "/search"]) == 1


def test_agent_auth_and_abilities(client: TestClient) -> None:
    assert client.post("/v1/tasks", json=web_search()).status_code == 401
    assert client.post("/v1/tasks", json=web_search(), headers=bearer("awv_wrong")).status_code == 401
    agent, token = make_agent(client)
    crawl = {"operation": "crawl", "input": {"collection_id": "x", "seeds": ["http://site.test/"]}}
    denied = client.post("/v1/tasks", json=crawl, headers=bearer(token))
    assert denied.status_code == 403 and denied.json()["error"]["code"] == "AUTH_SCOPE_DENIED"
    client.post(f"/api/agents/{agent['id']}/revoke", headers=UI)
    assert client.get("/v1/me", headers=bearer(token)).status_code == 401
    rotated = client.post(f"/api/agents/{agent['id']}/rotate", headers=UI).json()
    assert client.get("/v1/me", headers=bearer(rotated["token"])).status_code == 200


def test_tasks_of_other_agents_are_hidden(client: TestClient) -> None:
    fake("tavily", tavily_ok)
    add_key(client, "tavily", allowance=100, period="monthly")
    _a, t1 = make_agent(client, name="one")
    _b, t2 = make_agent(client, name="two")
    task_id = client.post("/v1/tasks", json=web_search(), headers=bearer(t1)).json()["task_id"]
    assert client.get(f"/v1/tasks/{task_id}", headers=bearer(t1)).status_code == 200
    assert client.get(f"/v1/tasks/{task_id}", headers=bearer(t2)).status_code == 404


# --------------------------------------------------------------------------- dashboard security


def test_dashboard_api_requires_header_host_and_same_origin(client: TestClient) -> None:
    assert client.get("/api/overview").status_code == 403
    assert client.get("/api/overview", headers=UI).status_code == 200
    assert client.get("/api/overview", headers={**UI, "Origin": "https://evil.example"}).status_code == 403
    assert client.get("/api/overview", headers={**UI, "Host": "evil.example"}).status_code == 421
    page = client.get("/")
    assert page.status_code == 200 and "AgentWebView" in page.text
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
    # agent tokens are not accepted by the dashboard API and the dashboard header is not an agent credential
    assert client.post("/v1/tasks", json=web_search(), headers=UI).status_code == 401


def test_provider_keys_are_encrypted_at_rest(client: TestClient, service: Service) -> None:
    fake("exa", lambda req: httpx.Response(200, json={"results": []}))
    add_key(client, "exa", key="sk-exa-very-secret-value")
    raw = service.config.db_path.read_bytes()
    for suffix in ("-wal", "-shm"):
        p = service.config.db_path.with_name(service.config.db_path.name + suffix)
        if p.exists():
            raw += p.read_bytes()
    assert b"sk-exa-very-secret-value" not in raw
    listed = client.get("/api/providers", headers=UI).text
    assert "very-secret" not in listed


# --------------------------------------------------------------------------- documents, page reading, crawling


def test_documents_upload_search_and_read(client: TestClient) -> None:
    col = client.post("/api/collections", json={"name": "notes"}, headers=UI).json()
    files = {"files": ("guide.md", b"# Refund policy\n\nRefunds are issued within 14 days of purchase.\n", "text/markdown")}
    up = client.post(f"/api/collections/{col['id']}/upload", files=files, headers=UI).json()
    assert up["results"][0]["ok"], up
    r = client.post(
        "/api/try", json={"operation": "search", "input": {"query": "how long do refunds take"}}, headers=UI
    ).json()
    assert r["status"] == "succeeded" and r["engine"] == "owned_corpus_search"
    assert "14 days" in r["evidence"][0]["excerpt"]
    doc_id = r["evidence"][0]["document_id"]
    read = client.post("/api/try", json={"operation": "read_page", "input": {"source_id": doc_id}}, headers=UI).json()
    assert read["status"] == "succeeded" and "Refunds are issued" in read["content"]
    rejected = client.post(
        f"/api/collections/{col['id']}/upload", files={"files": ("x.pdf", b"%PDF-1.4 binary", "application/pdf")}, headers=UI
    ).json()
    assert not rejected["results"][0]["ok"]


def test_direct_page_read_and_private_network_refusal(client: TestClient, site: dict[str, Any]) -> None:
    paragraphs = [
        "AgentWebView reads public pages through a guarded connection and extracts the main text.",
        "Every address is resolved once and checked to be public before the connection is opened.",
        "Redirects are followed by hand, so each hop is checked again against the same rules.",
        "Bodies are capped in size, and compressed responses are refused when they expand too far.",
        "The extracted text is returned with its source, its retrieval time and a version digest.",
    ]
    body = "<html><head><title>Test page</title></head><body><article>" + "".join(
        f"<p>{p}</p>" for p in paragraphs
    ) + "</article></body></html>"
    site["pages"]["/robots.txt"] = (200, "text/plain", "User-agent: *\nAllow: /\n")
    site["pages"]["/page"] = (200, "text/html; charset=utf-8", body)
    r = client.post("/api/try", json={"operation": "read_page", "input": {"url": "http://site.test/page"}}, headers=UI).json()
    assert r["status"] == "succeeded", r
    assert r["engine"] == "static_html_reader" and "guarded connection" in r["content"]
    blocked = client.post(
        "/api/try", json={"operation": "read_page", "input": {"url": f"http://127.0.0.1:{site['port']}/page"}}, headers=UI
    ).json()
    assert blocked["status"] == "failed" and blocked["error"]["code"] == "NETWORK_BLOCKED"
    assert "loopback" in blocked["error"]["message"] or "port" in blocked["error"]["message"]


def test_crawl_stores_pages_and_respects_robots(client: TestClient, site: dict[str, Any]) -> None:
    p = site["pages"]
    p["/robots.txt"] = (200, "text/plain", "User-agent: *\nDisallow: /private\n")
    long = "Widgets are assembled by hand in our workshop and tested twice before shipping. " * 4
    p["/"] = (200, "text/html", f"<html><body><h1>Home</h1><p>{long}</p><a href='/a'>A</a><a href='/private'>P</a><a href='https://other.example/'>X</a></body></html>")
    p["/a"] = (200, "text/html", f"<html><body><h1>Page A</h1><p>Gizmos ship worldwide. {long}</p></body></html>")
    p["/private"] = (200, "text/html", "<html><body>secret</body></html>")
    col = client.post("/api/collections", json={"name": "site"}, headers=UI).json()
    start = client.post("/api/crawls", json={"collection_id": col["id"], "seeds": ["http://site.test/"], "max_pages": 10}, headers=UI).json()
    assert start["status"] == "running"
    deadline = time.time() + 20
    while time.time() < deadline:
        t = client.get(f"/api/tasks/{start['task_id']}", headers=UI).json()
        if t["status"] != "running":
            break
        time.sleep(0.2)
    assert t["status"] == "succeeded", t
    assert t["coverage"]["pages_stored"] == 2
    docs = client.get(f"/api/collections/{col['id']}/documents", headers=UI).json()["documents"]
    assert sorted(d["local_identifier"] for d in docs) == ["http://site.test/", "http://site.test/a"]
    found = client.post("/api/try", json={"operation": "search", "input": {"query": "gizmos worldwide"}}, headers=UI).json()
    assert found["evidence"][0]["local_identifier"] == "http://site.test/a"


# --------------------------------------------------------------------------- MCP


def _rpc(client: TestClient, token: str, method: str, params: dict[str, Any], rid: int = 1) -> dict[str, Any]:
    res = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": rid, "method": method, "params": params},
        headers={**bearer(token), "Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2025-06-18"},
    )
    assert res.status_code == 200, res.text
    return res.json()


def test_mcp_tools_over_http(client: TestClient) -> None:
    fake("tavily", tavily_ok)
    add_key(client, "tavily", allowance=100, period="monthly")
    _agent, token = make_agent(client)
    assert client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).status_code == 401
    init = _rpc(
        client,
        token,
        "initialize",
        {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}},
    )
    assert init["result"]["serverInfo"]["name"] == "agentwebview"
    tools = _rpc(client, token, "tools/list", {}, 2)["result"]["tools"]
    assert {t["name"] for t in tools} == {
        "search", "read_page", "crawl_site", "add_document", "browser_action", "task_status", "routing_info", "rate_result",
    }
    out = _rpc(client, token, "tools/call", {"name": "search", "arguments": {"query": "mcp", "scope": "web"}}, 3)["result"]
    assert not out.get("isError"), out
    payload = out.get("structuredContent") or __import__("json").loads(out["content"][0]["text"])
    assert payload["engine"] == "tavily" and "trace" not in payload
    rated = _rpc(
        client, token, "tools/call", {"name": "rate_result", "arguments": {"task_id": payload["task_id"], "relevance": "relevant"}}, 4
    )["result"]
    assert not rated.get("isError"), rated
    denied = _rpc(client, token, "tools/call", {"name": "add_document", "arguments": {"collection": "c", "text": "x"}}, 5)["result"]
    assert denied.get("isError") and "AUTH_SCOPE_DENIED" in denied["content"][0]["text"]
