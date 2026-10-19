"""Browser sessions: one supervised Chromium per session, driven by queued typed actions.

Metering: opening a session reserves one request and the session's whole browser time (``browser_ms``) up
front; closing settles the time actually used. If the reservation is refused, the session does not start.

Termination: an independent supervisor thread kills the browser's whole process group at the session's hard
deadline, or when one action overruns its time by more than ``SUPERVISOR_TOLERANCE_MS``.

Write safety: a click that could submit, send, buy, delete or publish runs only with an agent-granted
capability (when Settings allow that) or after a person approves it on the Approvals screen. Later actions
wait for that decision. After an uncertain click the session stops and nothing is replayed.
"""

from __future__ import annotations

import base64
import contextlib
import logging
import multiprocessing as mp
import os
import signal
import threading
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from agentwebview import limits
from agentwebview.browser.actions import WRITE_CATEGORIES, classify_click, parse_action, redacted_params
from agentwebview.browser.proxy import PolicyProxy
from agentwebview.db import dumps, iso, loads, new_id, parse_dt
from agentwebview.egress import EgressPolicy
from agentwebview.errors import ErrorCode, GatewayError, invalid, not_found
from agentwebview.policy import host_allowed
from agentwebview.providers.adapters import PROVIDER_UNITS
from agentwebview.urls import UrlError, canonicalize, host_of, valid_host_pattern

log = logging.getLogger("agentwebview.browser")
SUPERVISOR_TOLERANCE_MS = 750
SESSION_BYTES_CAP = 50_000_000
IDLE_CLOSE_SECONDS = 300
ACTION_TIMEOUT_MS = 15_000
TERMINAL = ("succeeded", "failed", "uncertain", "denied", "expired")
MAX_INLINE_PNG = 3_000_000


def playwright_available() -> tuple[bool, str]:
    try:
        import playwright.sync_api  # noqa: F401
    except ImportError:
        return False, "Playwright is not installed: pip install 'agentwebview[browser]' && playwright install chromium"
    return True, ""


class Supervisor:
    """Kills the host process group when the current deadline passes."""

    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.deadline = float("inf")
        self.killed_at: float | None = None
        self._stop = threading.Event()
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self) -> None:
        while not self._stop.wait(0.05):
            if time.monotonic() > self.deadline:
                self.kill()
                return

    def kill(self) -> None:
        if self.killed_at is not None:
            return
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            if hasattr(os, "killpg"):
                os.killpg(self.pid, signal.SIGKILL)
            else:  # pragma: no cover - non-POSIX
                os.kill(self.pid, signal.SIGTERM)
        self.killed_at = time.monotonic()

    def stop(self) -> None:
        self._stop.set()


class BrowserHandle:
    def __init__(self, config: Any, allowed_hosts: set[str], direct_web: Any, hard_deadline_wall: float) -> None:
        def allow(host: str) -> bool:
            return host in allowed_hosts and host_allowed(direct_web, host)

        self.proxy = PolicyProxy(EgressPolicy.from_config(config), allow, max_bytes=SESSION_BYTES_CAP)
        port = self.proxy.start()
        mpctx = mp.get_context("spawn")
        self.conn, child = mpctx.Pipe()
        from agentwebview.browser.host import host_main

        sandbox = os.environ.get("AGENTWEBVIEW_CHROMIUM_NO_SANDBOX", "") not in ("1", "true", "yes")
        self.proc = mpctx.Process(
            target=host_main,
            daemon=False,
            args=(
                child,
                port,
                os.getpid(),
                hard_deadline_wall + SUPERVISOR_TOLERANCE_MS / 1000,
                sandbox,
                os.environ.get("AGENTWEBVIEW_CHROMIUM") or None,
            ),
        )
        self.proc.start()
        assert self.proc.pid is not None
        self.supervisor = Supervisor(self.proc.pid)
        ready = self._recv(45.0)
        if not ready or not ready.get("ok"):
            self.terminate()
            raise GatewayError(
                ErrorCode.UNSUPPORTED_ENVIRONMENT,
                f"the browser failed to start: {(ready or {}).get('error') or 'no answer'}",
                remediation="install it with: playwright install chromium",
            )
        self.browser_version = ready.get("browser_version")

    def _recv(self, seconds: float) -> dict[str, Any] | None:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if self.supervisor.killed_at is not None:
                return None
            try:
                if self.conn.poll(0.05):
                    return self.conn.recv()  # type: ignore[no-any-return]
            except (EOFError, OSError):
                return None
        return None

    def call(self, op: str, timeout_ms: int, **params: Any) -> dict[str, Any] | None:
        """Send one command. None if the host died or did not answer in time (then it is killed)."""
        if self.supervisor.killed_at is not None:
            return None
        previous = self.supervisor.deadline
        self.supervisor.deadline = min(previous, time.monotonic() + (timeout_ms + SUPERVISOR_TOLERANCE_MS) / 1000)
        try:
            self.conn.send({"op": op, "timeout_ms": timeout_ms, **params})
            reply = self._recv((timeout_ms + SUPERVISOR_TOLERANCE_MS) / 1000)
        except (OSError, EOFError):
            reply = None
        finally:
            if self.supervisor.killed_at is None:
                self.supervisor.deadline = previous
        if reply is None:
            self.supervisor.kill()
        return reply

    def terminate(self) -> None:
        if self.supervisor.killed_at is None and self.proc.is_alive():
            self.call("close", 5_000)
        self.supervisor.kill()
        self.supervisor.stop()
        self.proc.join(5)
        self.proxy.stop()


def _pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        return True
    return True


def reply_view(reply: dict[str, Any] | None) -> dict[str, Any]:
    if reply is None:
        return {"ok": False, "error": "no reply (browser terminated)"}
    out = {k: v for k, v in reply.items() if k not in ("png_b64", "html")}
    if isinstance(out.get("text"), str) and len(out["text"]) > 100_000:
        out["text"] = out["text"][:100_000]
    return out


# =========================================================================== one session


class SessionRunner(threading.Thread):
    def __init__(self, manager: BrowserManager, session_id: str, reservation_id: str, deadline_s: float) -> None:
        super().__init__(name=f"browser-{session_id[-8:]}", daemon=True)
        self.manager = manager
        self.app = manager.app
        self.session_id = session_id
        self.reservation_id = reservation_id
        self.deadline_mono = time.monotonic() + deadline_s
        self.deadline_wall = time.time() + deadline_s
        self.close_requested: str | None = None
        self.typed_on_page = False
        self.started = time.monotonic()

    # ------------------------------------------------------------------ db helpers

    def _session(self) -> Any:
        return self.app.db.read().execute("SELECT * FROM browser_sessions WHERE id = ?", (self.session_id,)).fetchone()

    def _set_session(self, **fields: Any) -> None:
        cols = ", ".join(f"{k} = ?" for k in fields)
        with self.app.db.tx() as conn:
            conn.execute(f"UPDATE browser_sessions SET {cols} WHERE id = ?", (*fields.values(), self.session_id))  # noqa: S608

    def _next_action(self) -> dict[str, Any] | None:
        with self.app.db.tx() as conn:
            row = conn.execute(
                "SELECT * FROM browser_actions WHERE session_id = ? AND state IN ('queued', 'approved', "
                "'awaiting_decision') ORDER BY seq LIMIT 1",
                (self.session_id,),
            ).fetchone()
            if row is None:
                return None
            if row["state"] == "awaiting_decision":
                timeout = self.app.policy().browser.decision_timeout_s
                created = parse_dt(row["created_at"]) or datetime.now(UTC)
                if datetime.now(UTC) - created > timedelta(seconds=timeout):
                    conn.execute(
                        "UPDATE browser_actions SET state = 'expired', finished_at = ?, error = ? WHERE id = ?",
                        (
                            iso(datetime.now(UTC)),
                            dumps({"code": "DECISION_REQUIRED", "message": "nobody decided in time; not run"}),
                            row["id"],
                        ),
                    )
                return None  # strict ordering: later actions wait for the decision
            conn.execute("UPDATE browser_actions SET state = 'running' WHERE id = ?", (row["id"],))
            return dict(row)

    def _finish(
        self,
        action_id: str,
        state: str,
        result: dict[str, Any] | None = None,
        error: GatewayError | None = None,
        *,
        params: dict[str, Any] | None = None,
        screenshot: str | None = None,
        element: dict[str, Any] | None = None,
    ) -> None:
        with self.app.db.tx() as conn:
            conn.execute(
                "UPDATE browser_actions SET state = ?, result = ?, error = ?, finished_at = ?, params = "
                "COALESCE(?, params), screenshot = COALESCE(?, screenshot), element = COALESCE(?, element), "
                "page_url = COALESCE(?, page_url) WHERE id = ?",
                (
                    state,
                    dumps(result) if result is not None else None,
                    dumps(error.to_dict()) if error is not None else None,
                    iso(datetime.now(UTC)),
                    dumps(redacted_params(params)) if params is not None else None,
                    screenshot,
                    dumps(element) if element is not None else None,
                    (result or {}).get("url"),
                    action_id,
                ),
            )
            if result and result.get("url"):
                conn.execute(
                    "UPDATE browser_sessions SET current_url = ?, title = ?, last_action_at = ? WHERE id = ?",
                    (result.get("url"), result.get("title"), iso(datetime.now(UTC)), self.session_id),
                )

    # ------------------------------------------------------------------ main loop

    def run(self) -> None:
        sess = self._session()
        allowed = set(loads(sess["allowed_hosts"], []))
        capabilities = set(loads(sess["capabilities"], []))
        policy = self.app.policy()
        handle: BrowserHandle | None = None
        reason = "closed"
        state = "closed"
        try:
            try:
                handle = BrowserHandle(self.app.config, allowed, policy.direct_web, self.deadline_wall)
            except GatewayError as exc:
                self._set_session(state="failed", close_reason=exc.message[:300])
                self._expire_pending("the browser did not start")
                self._finalize("failed", exc.message, error=exc)
                return
            self._set_session(state="active", last_action_at=iso(datetime.now(UTC)))
            last_activity = time.monotonic()
            while True:
                if self.close_requested:
                    reason = self.close_requested
                    break
                if time.monotonic() >= self.deadline_mono:
                    reason = "session time limit reached"
                    break
                if time.monotonic() - last_activity > IDLE_CLOSE_SECONDS:
                    reason = f"idle for {IDLE_CLOSE_SECONDS} seconds"
                    break
                row = self._next_action()
                if row is None:
                    time.sleep(0.1)
                    continue
                last_activity = time.monotonic()
                if row["type"] == "close":
                    self._finish(row["id"], "succeeded", {"ok": True, "closed": True})
                    reason = "closed by the agent"
                    break
                outcome = self._execute(handle, row, capabilities)
                if outcome == "dead":
                    reason, state = "the browser stopped responding", "failed"
                    break
                if outcome == "uncertain":
                    reason, state = "an action had an unknown outcome; the session stopped", "failed"
                    break
        except Exception as exc:  # the session must always close and settle
            log.exception("browser session %s failed", self.session_id)
            reason, state = f"internal error: {type(exc).__name__}", "failed"
        finally:
            if handle is not None:
                with contextlib.suppress(Exception):
                    handle.terminate()
            self._expire_pending("the session ended before this action ran")
            if handle is not None:
                self._set_session(state=state, close_reason=reason[:300], closed_at=iso(datetime.now(UTC)))
                self._finalize("succeeded" if state == "closed" else "failed", reason)
            self.manager._forget(self.session_id)

    def _expire_pending(self, why: str) -> None:
        with self.app.db.tx() as conn:
            conn.execute(
                "UPDATE browser_actions SET state = 'expired', finished_at = ?, error = ? WHERE session_id = ? AND "
                "state IN ('queued', 'approved', 'awaiting_decision', 'running')",
                (iso(datetime.now(UTC)), dumps({"code": "CANCELED", "message": why}), self.session_id),
            )

    def _finalize(self, status: str, reason: str, error: GatewayError | None = None) -> None:
        used_ms = int((time.monotonic() - self.started) * 1000)
        reserved = loads(
            self.app.db.read().execute("SELECT amounts FROM reservations WHERE id = ?", (self.reservation_id,))
            .fetchone()["amounts"],
            {},
        )
        used_ms = min(used_ms, int(reserved.get("browser_ms", used_ms)))
        usage = {"task_calls": 1, "browser_ms": used_ms}
        limits.settle(self.app.db, self.reservation_id, usage, datetime.now(UTC))
        sess = self._session()
        actions = self.manager.actions(self.session_id)
        result = {
            "task_id": sess["task_id"],
            "status": status,
            "operation": "browser_session",
            "engine": "local_chromium",
            "session_id": self.session_id,
            "coverage": {
                "kind": "live_destination",
                "allowed_hosts": loads(sess["allowed_hosts"], []),
                "close_reason": reason,
                "statement": "Browser traffic was restricted to the session's hosts; private networks were refused.",
            },
            "actions": actions,
            "resource_usage": {k: str(v) for k, v in usage.items()},
            "error": error.to_dict() if error else None,
            "completed_at": iso(datetime.now(UTC)),
            "retrieved_content_is_data": True,
        }
        with self.app.db.tx() as conn:
            conn.execute("UPDATE browser_sessions SET browser_ms = ? WHERE id = ?", (used_ms, self.session_id))
            conn.execute(
                "UPDATE tasks SET status = ?, engine_id = 'local_chromium', error_code = ?, usage = ?, result = ?, "
                "completed_at = ?, duration_ms = ? WHERE id = ?",
                (
                    status,
                    error.code.value if error else None,
                    dumps(usage),
                    dumps(result),
                    iso(datetime.now(UTC)),
                    used_ms,
                    sess["task_id"],
                ),
            )

    # ------------------------------------------------------------------ one action

    def _budget_ms(self) -> int:
        return max(500, min(ACTION_TIMEOUT_MS, int((self.deadline_mono - time.monotonic()) * 1000)))

    def _execute(self, handle: BrowserHandle, row: dict[str, Any], capabilities: set[str]) -> str:
        params = loads(row["params"], {})
        try:
            action = parse_action(params)
        except Exception as exc:
            self._finish(row["id"], "failed", error=GatewayError(ErrorCode.VALIDATION_FAILED, str(exc)[:300]), params=params)
            return "ok"
        budget = self._budget_ms()
        policy = self.app.policy()
        allowed = set(loads(self._session()["allowed_hosts"], []))
        if action.type == "navigate":
            try:
                host = host_of(canonicalize(action.url))
            except UrlError as exc:
                self._finish(row["id"], "failed", error=GatewayError(ErrorCode.VALIDATION_FAILED, str(exc)), params=params)
                return "ok"
            if host not in allowed or not host_allowed(policy.direct_web, host):
                self._finish(
                    row["id"],
                    "failed",
                    error=GatewayError(ErrorCode.NETWORK_BLOCKED, f"{host} is outside this session's allowed hosts"),
                    params=params,
                )
                return "ok"
            reply = handle.call("navigate", budget, url=action.url)
            if reply is None:
                self._finish(row["id"], "failed", error=GatewayError(ErrorCode.DEADLINE_EXCEEDED, "navigation timed out"))
                return "dead"
            blocked = reply.get("status") == 403 and bool(handle.proxy.stats.blocked)
            if not reply.get("ok") or blocked:
                code = ErrorCode.NETWORK_BLOCKED if blocked else ErrorCode.ENGINE_FAILURE
                msg = "destination blocked by egress policy" if blocked else str(reply.get("error"))[:300]
                self._finish(row["id"], "failed", reply_view(reply), GatewayError(code, msg))
                return "ok"
            self._finish(row["id"], "succeeded", reply_view(reply))
            self.typed_on_page = False
            return "ok"
        if action.type == "click":
            category = row.get("write_category")
            element = None
            if row["state"] != "approved":
                inspected = handle.call("inspect", min(budget, 5_000), selector=action.selector.model_dump())
                if inspected is None:
                    self._finish(row["id"], "failed", error=GatewayError(ErrorCode.DEADLINE_EXCEEDED, "inspection timed out"))
                    return "dead"
                if not inspected.get("ok"):
                    self._finish(
                        row["id"],
                        "failed",
                        error=GatewayError(ErrorCode.VALIDATION_FAILED, f"element not found: {inspected.get('error')}"),
                    )
                    return "ok"
                element = inspected.get("element") or {}
                category = classify_click(element, typed_on_page=self.typed_on_page)
                if category is not None:
                    mode = policy.browser.write_actions
                    if mode == "deny":
                        self._finish(
                            row["id"],
                            "failed",
                            {"write_category": category},
                            GatewayError(ErrorCode.AUTH_SCOPE_DENIED, f"Settings deny browser write actions ({category})"),
                            element=element,
                        )
                        return "ok"
                    granted = category in capabilities and mode == "allow_with_capability"
                    if not granted:
                        sess = self._session()
                        with self.app.db.tx() as conn:
                            conn.execute(
                                "UPDATE browser_actions SET state = 'awaiting_decision', write_category = ?, "
                                "element = ?, page_url = ? WHERE id = ?",
                                (category, dumps(element), sess["current_url"], row["id"]),
                            )
                        return "ok"
            reply = handle.call("click", self._budget_ms(), selector=action.selector.model_dump())
            if reply is None or reply.get("uncertain"):
                what = f"the {category} action" if category else "the click"
                self._finish(
                    row["id"],
                    "uncertain",
                    reply_view(reply),
                    GatewayError(ErrorCode.SESSION_STATE_UNKNOWN, f"{what} may or may not have taken effect; it will not be replayed"),
                    element=element,
                )
                return "uncertain"
            if reply.get("ok") and (category is not None or reply.get("navigated")):
                self.typed_on_page = False
            ok = bool(reply.get("ok"))
            self._finish(
                row["id"],
                "succeeded" if ok else "failed",
                {**reply_view(reply), "write_category": category},
                None if ok else GatewayError(ErrorCode.ENGINE_FAILURE, str(reply.get("error"))[:300]),
                element=element,
            )
            return "ok"
        call_params: dict[str, Any]
        if action.type == "fill":
            call_params = {"selector": action.selector.model_dump(), "value": action.value}
        elif action.type == "select":
            call_params = {"selector": action.selector.model_dump(), "values": action.values}
        elif action.type == "scroll":
            call_params = {"direction": action.direction, "pixels": action.pixels}
        elif action.type == "extract":
            call_params = {
                "selector": action.selector.model_dump() if action.selector else None,
                "mode": action.mode,
                "max_chars": action.max_chars,
            }
        else:
            call_params = {"full_page": action.full_page}
        reply = handle.call(action.type, budget, **call_params)
        if reply is None:
            self._finish(
                row["id"], "failed", error=GatewayError(ErrorCode.DEADLINE_EXCEEDED, f"{action.type} timed out"), params=params
            )
            return "dead"
        screenshot = None
        if action.type == "screenshot" and reply.get("ok"):
            png = base64.b64decode(reply.pop("png_b64"))
            if policy.browser.screenshots:
                path = self.app.config.artifacts_dir / f"{row['id']}.png"
                path.write_bytes(png)
                screenshot = path.name
                reply["screenshot_url"] = f"/api/screenshots/{row['id']}"
            reply["bytes"] = len(png)
            if action.inline and len(png) <= MAX_INLINE_PNG:
                reply["png_base64"] = base64.b64encode(png).decode()
        if action.type == "extract" and reply.get("html") is not None:
            from agentwebview.extract import RejectedImport, extract

            try:
                doc = extract(
                    "page.html", reply.pop("html").encode("utf-8"), declared_media_type="text/html",
                    original_url=reply.get("url"), max_chars=action.max_chars,
                )
                reply["text"], reply["title"] = doc.text, doc.title or reply.get("title")
            except RejectedImport as exc:
                reply["text"], reply["note"] = "", str(exc)
        ok = bool(reply.get("ok"))
        if ok and action.type in ("fill", "select"):
            self.typed_on_page = True
        view = reply_view(reply)
        if "png_base64" in reply:
            view["png_base64"] = reply["png_base64"]
        self._finish(
            row["id"],
            "succeeded" if ok else "failed",
            view,
            None if ok else GatewayError(ErrorCode.ENGINE_FAILURE, str(reply.get("error"))[:300]),
            params=params,
            screenshot=screenshot,
        )
        return "ok"


# =========================================================================== manager


class BrowserManager:
    def __init__(self, app: Any) -> None:
        self.app = app
        self._runners: dict[str, SessionRunner] = {}
        self._lock = threading.Lock()

    def _forget(self, session_id: str) -> None:
        with self._lock:
            self._runners.pop(session_id, None)

    def status(self) -> dict[str, Any]:
        ok, why = playwright_available()
        pol = self.app.policy().browser
        return {
            "enabled": pol.enabled,
            "installed": ok,
            "detail": why or None,
            "active_sessions": len(self._runners),
            "max_sessions": pol.max_sessions,
            "write_actions": pol.write_actions,
        }

    # ------------------------------------------------------------------ views

    def action_view(self, row: Any, *, include_png: bool = False) -> dict[str, Any]:
        result = loads(row["result"])
        if isinstance(result, dict) and not include_png:
            result = {k: v for k, v in result.items() if k != "png_base64"}
        return {
            "id": row["id"],
            "session_id": row["session_id"],
            "seq": row["seq"],
            "type": row["type"],
            "params": loads(row["params"], {}),
            "state": row["state"],
            "write_category": row["write_category"],
            "element": loads(row["element"]),
            "page_url": row["page_url"],
            "result": result,
            "error": loads(row["error"]),
            "screenshot_url": f"/api/screenshots/{row['id']}" if row["screenshot"] else None,
            "decision_note": row["decision_note"],
            "created_at": row["created_at"],
            "decided_at": row["decided_at"],
            "finished_at": row["finished_at"],
        }

    def actions(self, session_id: str) -> list[dict[str, Any]]:
        rows = self.app.db.read().execute(
            "SELECT * FROM browser_actions WHERE session_id = ? ORDER BY seq", (session_id,)
        ).fetchall()
        return [self.action_view(r) for r in rows]

    def session_view(self, row: Any, *, with_actions: bool = True) -> dict[str, Any]:
        out = {
            "id": row["id"],
            "task_id": row["task_id"],
            "agent_id": row["agent_id"],
            "state": row["state"],
            "start_url": row["start_url"],
            "allowed_hosts": loads(row["allowed_hosts"], []),
            "write_capabilities": loads(row["capabilities"], []),
            "current_url": row["current_url"],
            "title": row["title"],
            "browser_ms": row["browser_ms"],
            "created_at": row["created_at"],
            "closed_at": row["closed_at"],
            "close_reason": row["close_reason"],
        }
        if with_actions:
            out["actions"] = self.actions(row["id"])
        return out

    def get(self, session_id: str, agent_id: str | None = None) -> dict[str, Any]:
        row = self._row(session_id, agent_id)
        return self.session_view(row)

    def _row(self, session_id: str, agent_id: str | None) -> Any:
        row = self.app.db.read().execute("SELECT * FROM browser_sessions WHERE id = ?", (session_id,)).fetchone()
        if row is None or (agent_id is not None and row["agent_id"] != agent_id):
            raise not_found("browser session")
        return row

    def list(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.app.db.read().execute(
            "SELECT * FROM browser_sessions ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [self.session_view(r, with_actions=False) for r in rows]

    def pending_decisions(self) -> list[dict[str, Any]]:
        rows = self.app.db.read().execute(
            "SELECT a.* FROM browser_actions a JOIN browser_sessions s ON s.id = a.session_id "
            "WHERE a.state = 'awaiting_decision' ORDER BY a.created_at"
        ).fetchall()
        out = []
        for r in rows:
            view = self.action_view(r)
            sess = self.app.db.read().execute(
                "SELECT agent_id, current_url, title FROM browser_sessions WHERE id = ?", (r["session_id"],)
            ).fetchone()
            view["agent_id"] = sess["agent_id"] if sess else None
            view["page_title"] = sess["title"] if sess else None
            out.append(view)
        return out

    # ------------------------------------------------------------------ open / act / decide / close

    def open(
        self,
        *,
        agent_id: str | None,
        origin: str,
        start_url: str | None,
        allowed_hosts: list[str] | None,
        write_capabilities: list[str] | None,
        max_session_ms: int | None,
    ) -> dict[str, Any]:
        policy = self.app.policy()
        if not policy.browser.enabled:
            raise GatewayError(
                ErrorCode.CAPABILITY_UNSUPPORTED,
                "the browser is turned off in Settings",
                remediation="turn on the browser under Settings > Browser (needs Playwright and Chromium)",
            )
        ok, why = playwright_available()
        if not ok:
            raise GatewayError(ErrorCode.UNSUPPORTED_ENVIRONMENT, why)
        hosts: set[str] = set()
        for h in allowed_hosts or []:
            h = h.strip().lower().rstrip(".")
            if not valid_host_pattern(h):
                raise invalid(f"invalid host '{h}'")
            hosts.add(h)
        if start_url:
            try:
                hosts.add(host_of(canonicalize(start_url)))
            except UrlError as exc:
                raise invalid(f"invalid start_url: {exc}") from exc
        if not hosts:
            raise invalid("give a start_url or at least one allowed host")
        blocked = sorted(h for h in hosts if not host_allowed(policy.direct_web, h))
        if blocked:
            raise GatewayError(ErrorCode.NETWORK_BLOCKED, f"hosts not permitted by Settings: {', '.join(blocked)}")
        caps = sorted(set(write_capabilities or []))
        bad = [c for c in caps if c not in WRITE_CATEGORIES]
        if bad:
            raise invalid(f"unknown write capability: {', '.join(bad)} (use {', '.join(WRITE_CATEGORIES)})")
        with self._lock:
            if len(self._runners) >= policy.browser.max_sessions:
                raise GatewayError(
                    ErrorCode.RATE_LIMITED,
                    f"{len(self._runners)} browser session(s) already open (the limit is {policy.browser.max_sessions})",
                    retryable=True,
                    retry_after_ms=10_000,
                )
        session_ms = min(max_session_ms or policy.browser.max_session_ms, policy.browser.max_session_ms)
        now = datetime.now(UTC)
        task_id = self.app.create_task_row(
            agent_id=agent_id,
            origin=origin,
            operation="browser_session",
            summary=start_url or ", ".join(sorted(hosts)),
            request={"start_url": start_url, "allowed_hosts": sorted(hosts), "write_capabilities": caps,
                     "max_session_ms": session_ms},
        )
        try:
            reservation_id = limits.reserve(
                self.app.db,
                task_id=task_id,
                engine_id="local_chromium",
                amounts={"task_calls": 1, "browser_ms": session_ms},
                agent_id=agent_id,
                key_id=None,
                provider_units=PROVIDER_UNITS,
                now=now,
                tz=policy.timezone,
            )
        except GatewayError as exc:
            self.app.fail_task_row(task_id, exc)
            raise
        session_id = new_id("brs_")
        with self.app.db.tx() as conn:
            conn.execute(
                "INSERT INTO browser_sessions (id, task_id, agent_id, state, start_url, allowed_hosts, capabilities, "
                "owner_pid, created_at) VALUES (?, ?, ?, 'starting', ?, ?, ?, ?, ?)",
                (session_id, task_id, agent_id, start_url, dumps(sorted(hosts)), dumps(caps), os.getpid(), iso(now)),
            )
            if start_url:
                conn.execute(
                    "INSERT INTO browser_actions (id, session_id, seq, type, params, state, created_at) "
                    "VALUES (?, ?, 1, 'navigate', ?, 'queued', ?)",
                    (new_id("act_"), session_id, dumps({"type": "navigate", "url": start_url}), iso(now)),
                )
        runner = SessionRunner(self, session_id, reservation_id, session_ms / 1000)
        with self._lock:
            self._runners[session_id] = runner
        runner.start()
        # Wait for the browser to start and the first page to load.
        end = time.monotonic() + 60
        while time.monotonic() < end:
            row = self._row(session_id, None)
            first = self.app.db.read().execute(
                "SELECT state FROM browser_actions WHERE session_id = ? AND seq = 1", (session_id,)
            ).fetchone()
            if row["state"] == "failed" or (row["state"] == "active" and (first is None or first["state"] in TERMINAL)):
                break
            time.sleep(0.1)
        return self.session_view(self._row(session_id, None))

    def act(self, session_id: str, agent_id: str | None, action: dict[str, Any], wait_ms: int) -> dict[str, Any]:
        row = self._row(session_id, agent_id)
        if row["state"] not in ("starting", "active"):
            raise GatewayError(ErrorCode.SESSION_STATE_UNKNOWN, f"the session is {row['state']}; open a new one")
        try:
            parse_action(action)
        except Exception as exc:
            raise invalid(f"invalid action: {str(exc)[:300]}") from exc
        if session_id not in self._runners:
            raise GatewayError(
                ErrorCode.SESSION_STATE_UNKNOWN, "this session belongs to another AgentWebView process or has ended"
            )
        action_id = new_id("act_")
        with self.app.db.tx() as conn:
            seq = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 AS n FROM browser_actions WHERE session_id = ?", (session_id,)
            ).fetchone()["n"]
            conn.execute(
                "INSERT INTO browser_actions (id, session_id, seq, type, params, state, created_at) "
                "VALUES (?, ?, ?, ?, ?, 'queued', ?)",
                (action_id, session_id, seq, action["type"], dumps(action), iso(datetime.now(UTC))),
            )
        return self.wait_action(action_id, wait_ms)

    def wait_action(self, action_id: str, wait_ms: int) -> dict[str, Any]:
        end = time.monotonic() + max(0, wait_ms) / 1000
        while True:
            row = self.app.db.read().execute("SELECT * FROM browser_actions WHERE id = ?", (action_id,)).fetchone()
            if row is None:
                raise not_found("browser action")
            if row["state"] in (*TERMINAL, "awaiting_decision") or time.monotonic() >= end:
                view = self.action_view(row, include_png=True)
                if row["state"] == "awaiting_decision":
                    view["note"] = (
                        f"This '{row['write_category']}' action is waiting for a person to approve it on the "
                        "Approvals screen. Call browser_action with {\"type\": \"wait\", \"action_id\": ...} to "
                        "get its result after the decision."
                    )
                return view
            time.sleep(0.1)

    def decide(self, action_id: str, approve: bool, note: str | None) -> dict[str, Any]:
        with self.app.db.tx() as conn:
            row = conn.execute("SELECT * FROM browser_actions WHERE id = ?", (action_id,)).fetchone()
            if row is None:
                raise not_found("browser action")
            if row["state"] != "awaiting_decision":
                raise GatewayError(ErrorCode.CONFLICT, f"this action is {row['state']}; there is nothing to decide")
            now = iso(datetime.now(UTC))
            if approve:
                conn.execute(
                    "UPDATE browser_actions SET state = 'approved', decided_at = ?, decision_note = ? WHERE id = ?",
                    (now, (note or "")[:500] or None, action_id),
                )
            else:
                conn.execute(
                    "UPDATE browser_actions SET state = 'denied', decided_at = ?, finished_at = ?, decision_note = ?, "
                    "error = ? WHERE id = ?",
                    (
                        now,
                        now,
                        (note or "")[:500] or None,
                        dumps({"code": "AUTH_SCOPE_DENIED", "message": "a person denied this action"}),
                        action_id,
                    ),
                )
            row = conn.execute("SELECT * FROM browser_actions WHERE id = ?", (action_id,)).fetchone()
        return self.action_view(row)

    def close(self, session_id: str, agent_id: str | None, reason: str) -> dict[str, Any]:
        row = self._row(session_id, agent_id)
        runner = self._runners.get(session_id)
        if runner is not None:
            runner.close_requested = reason
            runner.join(15)
        elif row["state"] in ("starting", "active"):
            with self.app.db.tx() as conn:
                conn.execute(
                    "UPDATE browser_sessions SET state = 'closed', close_reason = ?, closed_at = ? WHERE id = ?",
                    ("closed (its process had ended)", iso(datetime.now(UTC)), session_id),
                )
        return self.session_view(self._row(session_id, None))

    def shutdown(self) -> None:
        for sid in list(self._runners):
            runner = self._runners.get(sid)
            if runner is not None:
                runner.close_requested = "AgentWebView is shutting down"
        for runner in list(self._runners.values()):
            runner.join(10)

    def recover(self) -> None:
        """At start-up: sessions left open by a process that has stopped are closed; their pending actions
        expire. Sessions owned by another running AgentWebView process (for example a stdio MCP server) are
        left alone."""
        now = iso(datetime.now(UTC))
        with self.app.db.tx() as conn:
            stale = [
                s
                for s in conn.execute(
                    "SELECT id, task_id, owner_pid FROM browser_sessions WHERE state IN ('starting', 'active')"
                ).fetchall()
                if not _pid_alive(s["owner_pid"]) or s["owner_pid"] == os.getpid()
            ]
            for s in stale:
                conn.execute(
                    "UPDATE browser_sessions SET state = 'closed', close_reason = 'AgentWebView restarted', closed_at = ? "
                    "WHERE id = ?",
                    (now, s["id"]),
                )
                conn.execute(
                    "UPDATE browser_actions SET state = 'expired', finished_at = ? WHERE session_id = ? AND "
                    "state IN ('queued', 'approved', 'awaiting_decision', 'running')",
                    (now, s["id"]),
                )
                conn.execute(
                    "UPDATE tasks SET status = 'failed', error_code = 'SESSION_STATE_UNKNOWN', completed_at = ? "
                    "WHERE id = ? AND status = 'running'",
                    (now, s["task_id"]),
                )
