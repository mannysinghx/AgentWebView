"""Test fixtures: a fresh data folder per test, fake provider APIs, and a local website reached through the real
egress guard (its name resolves to a public test address that is dialled to 127.0.0.1)."""

from __future__ import annotations

import http.server
import json
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from agentwebview.api import create_app
from agentwebview.config import Config
from agentwebview.providers.http import TEST_TRANSPORTS
from agentwebview.service import Service

PUBLIC_TEST_IP = "93.184.215.14"  # a public address; connections to it are redirected to the local test server
UI = {"X-AgentWebView": "1"}


class FakeProvider:
    """Answers one provider's API from a handler and counts calls."""

    def __init__(self, handler: Callable[[httpx.Request], httpx.Response]) -> None:
        self.handler = handler
        self.calls: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        return self.handler(request)


@pytest.fixture
def site() -> Iterator[dict[str, Any]]:
    pages: dict[str, tuple[int, str, str]] = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            status, ctype, body = pages.get(self.path, (404, "text/plain", "not found"))
            data = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args: Any) -> None:
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield {"pages": pages, "port": server.server_address[1]}
    server.shutdown()


@pytest.fixture
def service(tmp_path: Path, site: dict[str, Any]) -> Iterator[Service]:
    config = Config(
        data_dir=tmp_path / "data",
        test_mode=True,
        test_dns={"site.test": [PUBLIC_TEST_IP]},
        test_dial={f"{PUBLIC_TEST_IP}:80": f"127.0.0.1:{site['port']}"},
    )
    svc = Service(config)
    svc.update_policy({"crawl": {"min_host_delay_ms": 0}})
    yield svc
    TEST_TRANSPORTS.clear()
    svc.shutdown()


@pytest.fixture
def client(service: Service) -> Iterator[TestClient]:
    app = create_app(service)
    with TestClient(app, base_url="http://127.0.0.1:8718") as c:
        yield c


@pytest.fixture
def live_server(service: Service) -> Iterator[str]:
    """The real server on a random local port, for clients that make their own HTTP requests (SDKs)."""
    import time

    import uvicorn

    server = uvicorn.Server(uvicorn.Config(create_app(service), host="127.0.0.1", port=0, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.02)
    assert server.started, "the test server did not start"
    port = server.servers[0].sockets[0].getsockname()[1]
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(10)


def fake(provider_id: str, handler: Callable[[httpx.Request], httpx.Response]) -> FakeProvider:
    fp = FakeProvider(handler)
    TEST_TRANSPORTS[provider_id] = httpx.MockTransport(fp)
    return fp


def tavily_ok(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/usage":
        return httpx.Response(200, json={"key": {"usage": 10, "limit": 1000}, "account": {}})
    q = json.loads(request.content)["query"]
    return httpx.Response(
        200,
        json={
            "results": [
                {"url": "https://example.org/a", "title": f"About {q}", "content": f"{q} explained in detail", "score": 0.9},
                {"url": "https://example.org/b", "title": "Second", "content": "more text", "score": 0.5},
            ],
            "usage": {"credits": 1},
            "request_id": "r1",
        },
    )


def serpapi_ok(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/account.json":
        return httpx.Response(200, json={"plan_searches_left": 250, "searches_per_month": 250, "this_month_usage": 0})
    q = request.url.params.get("q")
    return httpx.Response(
        200,
        json={
            "organic_results": [{"position": 1, "link": "https://example.com/x", "title": f"{q} on Google", "snippet": f"{q} snippet"}],
            "search_metadata": {"id": "s1"},
        },
    )


def make_agent(client: TestClient, **body: Any) -> tuple[dict[str, Any], str]:
    res = client.post("/api/agents", json={"name": "test agent", **body}, headers=UI)
    assert res.status_code == 200, res.text
    data = res.json()
    return data["agent"], data["token"]


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def web_search(query: str = "model context protocol") -> dict[str, Any]:
    return {"operation": "search", "input": {"query": query}, "requirements": {"coverage": "web_index"}}
