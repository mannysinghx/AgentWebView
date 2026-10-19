"""The HTTP server: the dashboard, its API, the agent REST API (``/v1``), the MCP endpoint (``/mcp``) and the
drop-in compatible endpoints (``/compat/tavily``).

Security model for a dashboard without a login:

* The server listens on 127.0.0.1 unless you explicitly allow remote access.
* Requests whose Host header is not one of this server's names are refused (DNS-rebinding protection).
* Dashboard API calls must carry the ``X-AgentWebView: 1`` header, which a web page on another site cannot send
  without a CORS preflight this server never approves, and a cross-site Origin is refused.
* Agent endpoints (``/v1``, ``/mcp`` and ``/compat``) need an agent token; the dashboard API never accepts one.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from importlib import resources
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import Body, FastAPI, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from mcp.server.transport_security import TransportSecuritySettings
from starlette.routing import Route

from agentwebview import __version__, documents, mcp_server
from agentwebview.compat import tavily
from agentwebview.errors import ErrorCode, GatewayError, invalid
from agentwebview.service import Service

log = logging.getLogger("agentwebview.api")
UI_HEADER = "x-agentwebview"


def _web_dir() -> Path:
    return Path(str(resources.files("agentwebview") / "web"))


def create_app(service: Service) -> FastAPI:
    config = service.config
    mcp_server.configure(service)
    hosts = config.allowed_host_headers()
    mcp_app = mcp_server.mcp.streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[f"{h}:*" for h in hosts] + sorted(hosts),
            allowed_origins=[f"http://{h}:*" for h in hosts] + [f"https://{h}:*" for h in hosts],
        ),
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        service.startup()
        async with mcp_server.mcp.session_manager.run():
            yield
        service.shutdown()

    app = FastAPI(
        title="AgentWebView",
        version=__version__,
        description="A local gateway between AI agents and the tools they use: web search through your own provider "
        "keys (free credit first), page reading, your documents, crawling and a sandboxed browser, with limits and a "
        "receipt for every call.",
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url=None,
    )
    for route in mcp_app.routes:
        app.router.routes.append(route)
        if isinstance(route, Route) and route.path == "/mcp":
            app.router.routes.append(Route("/mcp/", endpoint=route.endpoint))
    app.include_router(tavily.router(service))

    # ------------------------------------------------------------------ middleware

    @app.middleware("http")
    async def guard(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        host = _host_name(request.headers.get("host") or "")
        if host not in hosts:
            return JSONResponse(
                {"error": {"code": "NETWORK_BLOCKED", "message": f"unexpected Host header '{host}'; add it with --extra-host"}},
                status_code=421,
            )
        path = request.url.path
        if path.startswith("/api/") and not path.startswith("/api/screenshots/"):
            origin = request.headers.get("origin")
            if origin and (urlsplit(origin).hostname or "") not in hosts:
                return JSONResponse({"error": {"code": "CSRF_FAILED", "message": "cross-site request refused"}}, status_code=403)
            if request.headers.get(UI_HEADER) != "1":
                return JSONResponse(
                    {"error": {"code": "CSRF_FAILED", "message": "dashboard API calls need the X-AgentWebView: 1 header"}},
                    status_code=403,
                )
        if path in ("/mcp", "/mcp/") and request.method == "POST":
            auth = request.headers.get("authorization") or ""
            token = auth[7:].strip() if auth.lower().startswith("bearer ") else None
            try:
                service.authenticate(token)
            except GatewayError as exc:
                return JSONResponse(
                    {"error": exc.to_dict()}, status_code=401, headers={"WWW-Authenticate": 'Bearer realm="AgentWebView"'}
                )
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        if path == "/" or path.startswith("/assets/"):
            response.headers.setdefault("X-Frame-Options", "DENY")
            response.headers.setdefault(
                "Content-Security-Policy",
                "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
                "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
            )
        return response

    @app.exception_handler(GatewayError)
    async def gateway_error(_request: Request, exc: GatewayError) -> JSONResponse:
        return JSONResponse({"error": exc.to_dict()}, status_code=exc.http_status)

    def agent_of(request: Request) -> dict[str, Any]:
        auth = request.headers.get("authorization") or ""
        token = auth[7:].strip() if auth.lower().startswith("bearer ") else None
        return service.authenticate(token)

    # ------------------------------------------------------------------ dashboard pages

    @app.get("/", include_in_schema=False)
    def index() -> HTMLResponse:
        return HTMLResponse((_web_dir() / "index.html").read_text("utf-8"))

    @app.get("/assets/{name}", include_in_schema=False)
    def asset(name: str) -> FileResponse:
        if name not in ("app.js", "styles.css", "logo.svg"):
            raise GatewayError(ErrorCode.NOT_FOUND, "not found")
        media = {"js": "text/javascript", "css": "text/css", "svg": "image/svg+xml"}[name.rsplit(".", 1)[1]]
        return FileResponse(_web_dir() / name, media_type=media, headers={"Cache-Control": "no-cache"})

    @app.get("/health", include_in_schema=False)
    def health() -> dict[str, Any]:
        return {"status": "ok", "version": __version__}

    # ================================================================== agent API (/v1)

    @app.get("/v1/me", tags=["agent"])
    def me(request: Request) -> dict[str, Any]:
        agent = agent_of(request)
        return service.get_agent(agent["id"])

    @app.post("/v1/tasks", tags=["agent"])
    def submit_task(request: Request, body: dict[str, Any] = Body(...)) -> JSONResponse:
        agent = agent_of(request)
        result = service.submit(body, agent=agent, origin="rest")
        return JSONResponse(result, status_code=202 if result.get("status") == "running" else 200)

    @app.get("/v1/tasks/{task_id}", tags=["agent"])
    def get_task(task_id: str, request: Request, wait_ms: int = 0) -> dict[str, Any]:
        agent = agent_of(request)
        return service.wait_task(task_id, agent["id"], wait_ms)

    @app.post("/v1/tasks/{task_id}/cancel", tags=["agent"])
    def cancel_task(task_id: str, request: Request) -> dict[str, Any]:
        agent = agent_of(request)
        return service.cancel_task(task_id, agent["id"])

    @app.post("/v1/tasks/{task_id}/rating", tags=["agent"])
    def rate_task(task_id: str, request: Request, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        agent = agent_of(request)
        return service.rate_task(
            task_id, agent["id"], str(body.get("relevance")), str(body.get("outcome") or "unknown"), body.get("notes")
        )

    @app.post("/v1/route/preview", tags=["agent"])
    def route_preview(request: Request, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        agent = agent_of(request)
        return service.route_preview(body, agent)

    @app.get("/v1/engines", tags=["agent"])
    def engines(request: Request) -> dict[str, Any]:
        agent_of(request)
        return {"engines": service.engines_view()}

    @app.get("/v1/collections", tags=["agent"])
    def agent_collections(request: Request) -> dict[str, Any]:
        agent_of(request)
        return {"collections": service.collections()}

    @app.post("/v1/collections/{ref}/documents", tags=["agent"])
    def agent_add_document(ref: str, request: Request, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        agent = agent_of(request)
        service.require(agent, "add_documents")
        text = str(body.get("text") or "")
        if not text.strip():
            raise invalid("text is required")
        title = body.get("title")
        col = service.ensure_collection(ref)
        raw = ((f"# {title}\n\n" if title else "") + text).encode("utf-8")
        return service.add_document(
            col["id"], str(body.get("filename") or "document.md"), raw, agent=agent, media_type="text/markdown",
            source_url=body.get("source_url"),
        )

    @app.post("/v1/browser/sessions", tags=["agent"])
    def open_session(request: Request, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        agent = agent_of(request)
        service.require(agent, "browser")
        return service.browser.open(
            agent_id=agent["id"],
            origin="rest",
            start_url=body.get("start_url"),
            allowed_hosts=body.get("allowed_hosts"),
            write_capabilities=body.get("write_capabilities"),
            max_session_ms=body.get("max_session_ms"),
        )

    @app.get("/v1/browser/sessions/{session_id}", tags=["agent"])
    def session_status(session_id: str, request: Request) -> dict[str, Any]:
        agent = agent_of(request)
        return service.browser.get(session_id, agent["id"])

    @app.post("/v1/browser/sessions/{session_id}/actions", tags=["agent"])
    def session_action(session_id: str, request: Request, body: dict[str, Any] = Body(...), wait_ms: int = 15_000) -> dict[str, Any]:
        agent = agent_of(request)
        service.require(agent, "browser")
        return service.browser.act(session_id, agent["id"], body, min(max(wait_ms, 0), 60_000))

    @app.get("/v1/browser/actions/{action_id}", tags=["agent"])
    def action_status(action_id: str, request: Request, wait_ms: int = 0) -> dict[str, Any]:
        agent = agent_of(request)
        row = service.db.read().execute(
            "SELECT s.agent_id FROM browser_actions a JOIN browser_sessions s ON s.id = a.session_id WHERE a.id = ?",
            (action_id,),
        ).fetchone()
        if row is None or row["agent_id"] != agent["id"]:
            raise GatewayError(ErrorCode.NOT_FOUND, "browser action not found")
        return service.browser.wait_action(action_id, min(max(wait_ms, 0), 60_000))

    @app.delete("/v1/browser/sessions/{session_id}", tags=["agent"])
    def close_session(session_id: str, request: Request) -> dict[str, Any]:
        agent = agent_of(request)
        return service.browser.close(session_id, agent["id"], "closed by the agent")

    # ================================================================== dashboard API (/api)

    @app.get("/api/overview", include_in_schema=False)
    def overview() -> dict[str, Any]:
        return service.overview()

    @app.get("/api/connect", include_in_schema=False)
    def connect() -> dict[str, Any]:
        return {
            "base_url": config.base_url,
            "mcp_url": config.base_url + "/mcp",
            "rest_url": config.base_url + "/v1",
            "data_dir": str(config.data_dir),
            "version": __version__,
        }

    # ---- providers

    @app.get("/api/providers", include_in_schema=False)
    def providers() -> dict[str, Any]:
        return {"providers": service.providers()}

    @app.post("/api/providers/{provider_id}/keys", include_in_schema=False)
    def add_key(provider_id: str, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        amount = body.get("allowance")
        return service.add_key(
            provider_id,
            str(body.get("key") or ""),
            label=body.get("label"),
            allowance=None if amount in (None, "") else int(amount),
            period=body.get("period") or None,
            expires_at=body.get("expires_at") or None,
        )

    @app.patch("/api/keys/{key_id}", include_in_schema=False)
    def update_key(key_id: str, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        return service.update_key(
            key_id, label=body.get("label"), enabled=body.get("enabled"), allowance=body.get("allowance")
        )

    @app.put("/api/keys/{key_id}/secret", include_in_schema=False)
    def replace_secret(key_id: str, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        return service.replace_key_secret(key_id, str(body.get("key") or ""))

    @app.post("/api/keys/{key_id}/check", include_in_schema=False)
    def check_key(key_id: str) -> dict[str, Any]:
        return service.check_key(key_id)

    @app.delete("/api/keys/{key_id}", include_in_schema=False)
    def delete_key(key_id: str) -> dict[str, Any]:
        service.delete_key(key_id)
        return {"deleted": True}

    # ---- agents

    @app.get("/api/agents", include_in_schema=False)
    def agents() -> dict[str, Any]:
        return {"agents": service.list_agents()}

    @app.post("/api/agents", include_in_schema=False)
    def create_agent(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        lim = body.get("limits") or {}
        agent, token = service.create_agent(
            str(body.get("name") or ""),
            body.get("abilities"),
            _int_or_none(lim.get("monthly")),
            _int_or_none(lim.get("daily")),
        )
        return {"agent": agent, "token": token}

    @app.patch("/api/agents/{agent_id}", include_in_schema=False)
    def update_agent(agent_id: str, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        lim = body.get("limits")
        return service.update_agent(
            agent_id,
            name=body.get("name"),
            abilities=body.get("abilities"),
            limits_patch=None
            if lim is None
            else {"monthly": _int_or_none(lim.get("monthly")), "daily": _int_or_none(lim.get("daily"))},
        )

    @app.post("/api/agents/{agent_id}/rotate", include_in_schema=False)
    def rotate(agent_id: str) -> dict[str, Any]:
        agent, token = service.rotate_token(agent_id)
        return {"agent": agent, "token": token}

    @app.post("/api/agents/{agent_id}/revoke", include_in_schema=False)
    def revoke(agent_id: str) -> dict[str, Any]:
        return service.revoke_agent(agent_id)

    @app.delete("/api/agents/{agent_id}", include_in_schema=False)
    def delete_agent(agent_id: str) -> dict[str, Any]:
        service.delete_agent(agent_id)
        return {"deleted": True}

    # ---- activity and tasks

    @app.get("/api/activity", include_in_schema=False)
    def activity(
        limit: int = 100, agent: str | None = None, operation: str | None = None, status: str | None = None,
        before: str | None = None,
    ) -> dict[str, Any]:
        return {
            "items": service.activity(limit=limit, agent_id=agent, operation=operation, status=status, before=before)
        }

    @app.get("/api/tasks/{task_id}", include_in_schema=False)
    def task(task_id: str) -> dict[str, Any]:
        out = service.get_task(task_id)
        row = service.db.read().execute(
            "SELECT t.request, t.origin, t.agent_id, a.name FROM tasks t LEFT JOIN agents a ON a.id = t.agent_id "
            "WHERE t.id = ?",
            (task_id,),
        ).fetchone()
        if row is not None:
            from agentwebview.db import loads

            out["request"] = loads(row["request"], {})
            out["origin"] = row["origin"]
            out["agent_name"] = row["name"]
        return out

    @app.post("/api/tasks/{task_id}/cancel", include_in_schema=False)
    def ui_cancel(task_id: str) -> dict[str, Any]:
        return service.cancel_task(task_id)

    @app.post("/api/tasks/{task_id}/rating", include_in_schema=False)
    def ui_rate(task_id: str, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        return service.rate_task(task_id, None, str(body.get("relevance")), str(body.get("outcome") or "unknown"))

    @app.post("/api/try", include_in_schema=False)
    def try_it(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        return service.submit(body, agent=None, origin="dashboard")

    @app.post("/api/route/preview", include_in_schema=False)
    def ui_route(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        return service.route_preview(body, None)

    @app.get("/api/engines", include_in_schema=False)
    def ui_engines() -> dict[str, Any]:
        return {"engines": service.engines_view()}

    # ---- documents

    @app.get("/api/collections", include_in_schema=False)
    def collections() -> dict[str, Any]:
        return {"collections": service.collections()}

    @app.post("/api/collections", include_in_schema=False)
    def create_collection(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        return service.create_collection(str(body.get("name") or ""), body.get("description"))

    @app.delete("/api/collections/{collection_id}", include_in_schema=False)
    def delete_collection(collection_id: str) -> dict[str, Any]:
        documents.delete_collection(service.db, collection_id)
        return {"deleted": True}

    @app.get("/api/collections/{collection_id}/documents", include_in_schema=False)
    def list_documents(collection_id: str) -> dict[str, Any]:
        return {"documents": documents.list_documents(service.db.read(), collection_id)}

    @app.post("/api/collections/{collection_id}/upload", include_in_schema=False)
    def upload(collection_id: str, files: list[UploadFile] = File(...)) -> dict[str, Any]:  # noqa: B008
        results = []
        for f in files[:50]:
            raw = f.file.read(documents.MAX_UPLOAD_BYTES + 1)
            try:
                doc = service.add_document(collection_id, f.filename or "upload.txt", raw, media_type=f.content_type)
                results.append({"file": f.filename, "ok": True, "document": doc})
            except GatewayError as exc:
                results.append({"file": f.filename, "ok": False, "error": exc.to_dict()})
        return {"results": results}

    @app.post("/api/collections/{collection_id}/text", include_in_schema=False)
    def add_text(collection_id: str, title: str = Form(""), text: str = Form(...)) -> dict[str, Any]:
        raw = ((f"# {title}\n\n" if title else "") + text).encode("utf-8")
        return service.add_document(collection_id, (title or "note")[:80] + ".md", raw, media_type="text/markdown")

    @app.delete("/api/documents/{document_id}", include_in_schema=False)
    def delete_document(document_id: str) -> dict[str, Any]:
        documents.delete_document(service.db, document_id)
        return {"deleted": True}

    @app.post("/api/crawls", include_in_schema=False)
    def start_crawl(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        return service.submit({"operation": "crawl", "input": body}, agent=None, origin="dashboard")

    # ---- browser and approvals

    @app.get("/api/approvals", include_in_schema=False)
    def approvals() -> dict[str, Any]:
        return {"pending": service.browser.pending_decisions(), "browser": service.browser.status()}

    @app.post("/api/approvals/{action_id}", include_in_schema=False)
    def decide(action_id: str, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        return service.browser.decide(action_id, bool(body.get("approve")), body.get("note"))

    @app.get("/api/browser/sessions", include_in_schema=False)
    def sessions() -> dict[str, Any]:
        return {"sessions": service.browser.list(), "browser": service.browser.status()}

    @app.get("/api/browser/sessions/{session_id}", include_in_schema=False)
    def session(session_id: str) -> dict[str, Any]:
        return service.browser.get(session_id)

    @app.post("/api/browser/sessions/{session_id}/close", include_in_schema=False)
    def ui_close(session_id: str) -> dict[str, Any]:
        return service.browser.close(session_id, None, "closed from the dashboard")

    @app.get("/api/screenshots/{action_id}", include_in_schema=False)
    def screenshot(action_id: str) -> FileResponse:
        row = service.db.read().execute("SELECT screenshot FROM browser_actions WHERE id = ?", (action_id,)).fetchone()
        if row is None or not row["screenshot"]:
            raise GatewayError(ErrorCode.NOT_FOUND, "screenshot not found")
        path = config.artifacts_dir / row["screenshot"]
        if not path.exists() or path.parent != config.artifacts_dir:
            raise GatewayError(ErrorCode.NOT_FOUND, "screenshot not found")
        return FileResponse(path, media_type="image/png", headers={"Cache-Control": "private, max-age=3600"})

    # ---- settings

    @app.get("/api/settings", include_in_schema=False)
    def settings() -> dict[str, Any]:
        ov = service.overview()
        return {
            "policy": service.policy().model_dump(mode="json"),
            "global_limits": ov["global_limits"],
            "browser": service.browser.status(),
            "data_dir": str(config.data_dir),
            "listen": f"{config.host}:{config.port}",
        }

    @app.patch("/api/settings", include_in_schema=False)
    def update_settings(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        return {"policy": service.update_policy(body).model_dump(mode="json")}

    @app.put("/api/limits", include_in_schema=False)
    def put_limit(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        return {
            "global_limits": service.set_global_limit(
                str(body.get("unit")), str(body.get("period") or "monthly"), _int_or_none(body.get("amount"))
            )
        }

    return app


def _host_name(value: str) -> str:
    """The host part of a Host header ("127.0.0.1:8718" -> "127.0.0.1", "[::1]:8718" -> "[::1]")."""
    value = value.strip().lower()
    if value.startswith("["):
        return value.split("]", 1)[0] + "]"
    return value.rsplit(":", 1)[0] if ":" in value else value


def _int_or_none(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise invalid(f"'{value}' is not a whole number") from exc
