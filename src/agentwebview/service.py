"""The application: everything the REST API, the MCP tools and the dashboard call.

No accounts and no login. The dashboard is for the person running AgentWebView on their own machine; agents
identify themselves with the token created for them on the Agents screen, and every agent call is checked
against that agent's abilities and limits.
"""

from __future__ import annotations

import hashlib
import logging
import os
import secrets
import threading
from datetime import UTC, datetime
from typing import Any

from agentwebview import documents, limits
from agentwebview.browser.sessions import BrowserManager, _pid_alive
from agentwebview.config import Config
from agentwebview.contract import CrawlTask, ReadPageTask, SearchTask, parse_task, summary_of
from agentwebview.crypto import KeyBox
from agentwebview.db import Database, dumps, iso, loads, new_id, parse_dt
from agentwebview.engine import CancelToken
from agentwebview.engines.builtin import OwnedCorpusSearch, StaticHtmlReader, validate_live_url
from agentwebview.engines.crawler import ScopedCrawler
from agentwebview.errors import ErrorCode, GatewayError, invalid, not_found
from agentwebview.executor import Executor
from agentwebview.extract import RejectedImport, extract
from agentwebview.policy import Policy
from agentwebview.providers.adapters import ENGINE_CLASSES, ENGINE_FOR_PROVIDER, SPECS, KeyCheckFailed
from agentwebview.providers.http import ProviderHTTP, ProviderTransportError
from agentwebview.routing import AccountOption, load_health, plan_route, reset_scope

log = logging.getLogger("agentwebview")

ABILITIES = ("search", "read_page", "add_documents", "crawl", "browser")
BASE_ABILITIES = ("search", "read_page")
TOKEN_PREFIX = "awv_"


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


class Service:
    def __init__(self, config: Config) -> None:
        config.ensure_dirs()
        self.config = config
        self.db = Database(config.db_path)
        self.keybox = KeyBox(config.key_path)
        limits.ensure_defaults(self.db)
        self._engines: list[Any] = [OwnedCorpusSearch(config), StaticHtmlReader(config), ScopedCrawler(config)]
        self._engines += [cls(config) for cls in ENGINE_CLASSES]
        self._by_id = {e.descriptor.id: e for e in self._engines}
        self.executor = Executor(self)
        self.browser = BrowserManager(self)
        self._running: dict[str, CancelToken] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ lifecycle

    def startup(self) -> None:
        now = datetime.now(UTC)
        released = limits.release_stale(self.db, now)
        with self.db.tx() as conn:
            for t in conn.execute("SELECT id, owner_pid FROM tasks WHERE status = 'running'").fetchall():
                if t["owner_pid"] == os.getpid() or _pid_alive(t["owner_pid"]):
                    continue
                conn.execute(
                    "UPDATE tasks SET status = 'failed', error_code = 'CANCELED', completed_at = ? WHERE id = ?",
                    (iso(now), t["id"]),
                )
                conn.execute(
                    "UPDATE crawls SET state = 'interrupted', stop_reason = 'AgentWebView stopped', finished_at = ? "
                    "WHERE id = ? AND state = 'running'",
                    (iso(now), t["id"]),
                )
        self.browser.recover()
        if released:
            log.info("released %d stale reservation(s)", released)

    def shutdown(self) -> None:
        for token in list(self._running.values()):
            token.cancel("AgentWebView is shutting down")
        self.browser.shutdown()

    # ------------------------------------------------------------------ settings

    def policy(self) -> Policy:
        raw = self.db.get_meta("policy")
        if not raw:
            return Policy()
        try:
            return Policy.model_validate(loads(raw, {}))
        except Exception:  # an invalid stored policy never stops the gateway: fall back to defaults
            log.exception("stored policy is invalid; using defaults")
            return Policy()

    def update_policy(self, patch: dict[str, Any]) -> Policy:
        from pydantic import ValidationError

        merged = _deep_merge(self.policy().model_dump(mode="json"), patch)
        try:
            pol = Policy.model_validate(merged)
        except ValidationError as exc:
            raise GatewayError(
                ErrorCode.VALIDATION_FAILED,
                "invalid settings",
                details={"errors": [f"{'.'.join(map(str, e['loc']))}: {e['msg']}"[:200] for e in exc.errors()[:10]]},
            ) from exc
        self.db.set_meta("policy", dumps(pol.model_dump(mode="json")))
        return pol

    def engines(self) -> list[Any]:
        return list(self._engines)

    def engine(self, engine_id: str) -> Any:
        try:
            return self._by_id[engine_id]
        except KeyError as exc:
            raise not_found(f"engine '{engine_id}'") from exc

    def engines_view(self) -> list[dict[str, Any]]:
        health = load_health(self.db.read())
        out = []
        for e in self._engines:
            h = health.get(e.descriptor.id)
            out.append(
                {
                    **e.descriptor.as_dict(),
                    "health": None
                    if h is None
                    else {
                        "circuit_state": h.circuit_state,
                        "open_until": iso(h.open_until),
                        "cooldown_until": iso(h.cooldown_until),
                        "latency_ewma_ms": None if h.latency_ewma_ms is None else round(h.latency_ewma_ms, 1),
                        "quality": None if h.quality is None else round(h.quality, 3),
                        "quality_basis": h.quality_basis,
                    },
                }
            )
        return out

    # ------------------------------------------------------------------ agents

    def _agent_view(self, row: Any) -> dict[str, Any]:
        conn = self.db.read()
        now = datetime.now(UTC)
        tz = self.policy().timezone
        lims = {"monthly": None, "daily": None}
        for r in conn.execute(
            "SELECT * FROM limits WHERE scope = 'agent' AND scope_id = ? AND denomination = 'task_calls'", (row["id"],)
        ).fetchall():
            lims[r["period"]] = limits.view(conn, r, now, tz)
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        counts = conn.execute(
            "SELECT COUNT(*) AS n, SUM(CASE WHEN status = 'succeeded' THEN 1 ELSE 0 END) AS ok FROM tasks "
            "WHERE agent_id = ? AND created_at >= ?",
            (row["id"], iso(month_start)),
        ).fetchone()
        status = "revoked" if row["revoked_at"] else ("connected" if row["last_used_at"] else "waiting")
        return {
            "id": row["id"],
            "name": row["name"],
            "token_hint": row["token_hint"],
            "abilities": loads(row["abilities"], list(BASE_ABILITIES)),
            "status": status,
            "created_at": row["created_at"],
            "last_used_at": row["last_used_at"],
            "revoked_at": row["revoked_at"],
            "limits": lims,
            "this_month": {"calls": int(counts["n"] or 0), "succeeded": int(counts["ok"] or 0)},
        }

    def list_agents(self) -> list[dict[str, Any]]:
        rows = self.db.read().execute("SELECT * FROM agents ORDER BY revoked_at IS NOT NULL, created_at DESC").fetchall()
        return [self._agent_view(r) for r in rows]

    def get_agent(self, agent_id: str) -> dict[str, Any]:
        row = self.db.read().execute("SELECT * FROM agents WHERE id = ?", (agent_id,)).fetchone()
        if row is None:
            raise not_found("agent")
        return self._agent_view(row)

    def _clean_abilities(self, abilities: list[str] | None) -> list[str]:
        chosen = set(BASE_ABILITIES) | set(abilities or [])
        bad = chosen - set(ABILITIES)
        if bad:
            raise invalid(f"unknown abilities: {', '.join(sorted(bad))}")
        return [a for a in ABILITIES if a in chosen]

    def _set_agent_limits(self, agent_id: str, monthly: int | None, daily: int | None) -> None:
        for period, amount in (("monthly", monthly), ("daily", daily)):
            if amount is not None and amount < 0:
                raise invalid("limits cannot be negative")
            limits.set_limit(
                self.db,
                scope="agent",
                scope_id=agent_id,
                denomination="task_calls",
                period=period,
                amount=amount,
                name=f"Agent {period} requests",
            )

    def create_agent(
        self, name: str, abilities: list[str] | None, monthly: int | None, daily: int | None
    ) -> tuple[dict[str, Any], str]:
        name = " ".join((name or "").split())[:120]
        if not name:
            raise invalid("give the agent a name, for example 'Claude Code on my laptop'")
        token = TOKEN_PREFIX + secrets.token_urlsafe(32)
        agent_id = new_id("agt_")
        with self.db.tx() as conn:
            conn.execute(
                "INSERT INTO agents (id, name, token_hash, token_hint, abilities, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    agent_id,
                    name,
                    _hash_token(token),
                    token[:7] + "…" + token[-4:],
                    dumps(self._clean_abilities(abilities)),
                    iso(datetime.now(UTC)),
                ),
            )
        self._set_agent_limits(agent_id, monthly, daily)
        return self.get_agent(agent_id), token

    def update_agent(
        self,
        agent_id: str,
        *,
        name: str | None = None,
        abilities: list[str] | None = None,
        limits_patch: dict[str, int | None] | None = None,
    ) -> dict[str, Any]:
        self.get_agent(agent_id)
        with self.db.tx() as conn:
            if name is not None:
                clean = " ".join(name.split())[:120]
                if not clean:
                    raise invalid("the name cannot be empty")
                conn.execute("UPDATE agents SET name = ? WHERE id = ?", (clean, agent_id))
            if abilities is not None:
                conn.execute(
                    "UPDATE agents SET abilities = ? WHERE id = ?", (dumps(self._clean_abilities(abilities)), agent_id)
                )
        if limits_patch is not None:
            self._set_agent_limits(agent_id, limits_patch.get("monthly"), limits_patch.get("daily"))
        return self.get_agent(agent_id)

    def revoke_agent(self, agent_id: str) -> dict[str, Any]:
        self.get_agent(agent_id)
        with self.db.tx() as conn:
            conn.execute(
                "UPDATE agents SET revoked_at = COALESCE(revoked_at, ?) WHERE id = ?", (iso(datetime.now(UTC)), agent_id)
            )
        return self.get_agent(agent_id)

    def delete_agent(self, agent_id: str) -> None:
        self.get_agent(agent_id)
        with self.db.tx() as conn:
            conn.execute("DELETE FROM limits WHERE scope = 'agent' AND scope_id = ?", (agent_id,))
            conn.execute("DELETE FROM agents WHERE id = ?", (agent_id,))

    def rotate_token(self, agent_id: str) -> tuple[dict[str, Any], str]:
        self.get_agent(agent_id)
        token = TOKEN_PREFIX + secrets.token_urlsafe(32)
        with self.db.tx() as conn:
            conn.execute(
                "UPDATE agents SET token_hash = ?, token_hint = ?, revoked_at = NULL WHERE id = ?",
                (_hash_token(token), token[:7] + "…" + token[-4:], agent_id),
            )
        return self.get_agent(agent_id), token

    def authenticate(self, token: str | None) -> dict[str, Any]:
        if not token or not token.startswith(TOKEN_PREFIX):
            raise GatewayError(
                ErrorCode.AUTH_REQUIRED,
                "send the agent's token as 'Authorization: Bearer awv_…' (create one on the Agents screen)",
            )
        row = self.db.read().execute("SELECT * FROM agents WHERE token_hash = ?", (_hash_token(token),)).fetchone()
        if row is None or row["revoked_at"] is not None:
            raise GatewayError(ErrorCode.AUTH_REQUIRED, "this agent token is unknown or was revoked")
        now = datetime.now(UTC)
        last = parse_dt(row["last_used_at"])
        if last is None or (now - last).total_seconds() > 30:
            with self.db.tx() as conn:
                conn.execute("UPDATE agents SET last_used_at = ? WHERE id = ?", (iso(now), row["id"]))
        return {"id": row["id"], "name": row["name"], "abilities": loads(row["abilities"], list(BASE_ABILITIES))}

    @staticmethod
    def require(agent: dict[str, Any] | None, ability: str) -> None:
        if agent is not None and ability not in agent["abilities"]:
            raise GatewayError(
                ErrorCode.AUTH_SCOPE_DENIED,
                f"this agent may not use '{ability}'",
                remediation="allow it for this agent on the Agents screen",
            )

    # ------------------------------------------------------------------ provider keys

    def _key_view(self, row: Any) -> dict[str, Any]:
        spec = SPECS[row["provider_id"]]
        conn = self.db.read()
        now = datetime.now(UTC)
        tz = self.policy().timezone
        allowance = [
            limits.view(conn, r, now, tz)
            for r in conn.execute(
                "SELECT * FROM limits WHERE scope = 'provider_key' AND scope_id = ?", (row["id"],)
            ).fetchall()
        ]
        health = load_health(conn).get(f"{ENGINE_FOR_PROVIDER[row['provider_id']]}@key:{row['id']}")
        blocked_until = None
        if health is not None:
            for until in (health.open_until if health.circuit_state == "OPEN" else None, health.cooldown_until):
                if until and until > now:
                    blocked_until = iso(until)
        return {
            "id": row["id"],
            "provider_id": row["provider_id"],
            "provider_name": spec.name,
            "label": row["label"],
            "key_hint": row["key_hint"],
            "status": row["status"],
            "status_detail": row["status_detail"],
            "checked_at": row["checked_at"],
            "balance": loads(row["balance"]),
            "enabled": bool(row["enabled"]),
            "last_used_at": row["last_used_at"],
            "created_at": row["created_at"],
            "unit": spec.unit,
            "unit_label": spec.unit_label,
            "allowance": allowance[0] if allowance else None,
            "paused_until": blocked_until,
        }

    def providers(self) -> list[dict[str, Any]]:
        rows = self.db.read().execute("SELECT * FROM provider_keys ORDER BY created_at").fetchall()
        keys: dict[str, list[dict[str, Any]]] = {}
        for r in rows:
            keys.setdefault(r["provider_id"], []).append(self._key_view(r))
        return [{**spec.as_dict(), "keys": keys.get(pid, [])} for pid, spec in SPECS.items()]

    def get_key(self, key_id: str) -> dict[str, Any]:
        row = self.db.read().execute("SELECT * FROM provider_keys WHERE id = ?", (key_id,)).fetchone()
        if row is None:
            raise not_found("provider key")
        return self._key_view(row)

    def _set_allowance(
        self, key_id: str, provider_id: str, amount: int | None, period: str | None, expires_at: str | None
    ) -> None:
        spec = SPECS[provider_id]
        if period not in (None, "monthly", "daily", "lifetime"):
            raise invalid("the allowance renews 'monthly', 'daily' or never ('lifetime')")
        if expires_at:
            exp = parse_dt(expires_at)
            if exp is None:
                raise invalid("expires_at must be a date")
            expires_at = iso(exp)
        with self.db.tx() as conn:
            existing = conn.execute(
                "SELECT * FROM limits WHERE scope = 'provider_key' AND scope_id = ?", (key_id,)
            ).fetchall()
            if amount is None:
                conn.execute("DELETE FROM limits WHERE scope = 'provider_key' AND scope_id = ?", (key_id,))
                return
            if amount < 0:
                raise invalid("the allowance cannot be negative")
            period = period or "monthly"
            keep = [r for r in existing if r["period"] == period]
            for r in existing:
                if r["period"] != period:
                    conn.execute("DELETE FROM limits WHERE id = ?", (r["id"],))
            if keep:
                conn.execute(
                    "UPDATE limits SET amount = ?, expires_at = ? WHERE id = ?", (int(amount), expires_at, keep[0]["id"])
                )
            else:
                conn.execute(
                    "INSERT INTO limits (id, scope, scope_id, denomination, period, amount, expires_at, name, created_at) "
                    "VALUES (?, 'provider_key', ?, ?, ?, ?, ?, ?, ?)",
                    (
                        new_id("lim_"),
                        key_id,
                        spec.unit,
                        period,
                        int(amount),
                        expires_at,
                        f"{spec.name} allowance",
                        iso(datetime.now(UTC)),
                    ),
                )

    def add_key(
        self,
        provider_id: str,
        key: str,
        *,
        label: str | None = None,
        allowance: int | None = None,
        period: str | None = None,
        expires_at: str | None = None,
        use_default_allowance: bool = True,
    ) -> dict[str, Any]:
        spec = SPECS.get(provider_id)
        if spec is None:
            raise not_found(f"provider '{provider_id}'")
        key = (key or "").strip()
        if len(key) < 8 or len(key) > 500 or any(c.isspace() for c in key):
            raise invalid("paste the API key exactly as the provider shows it")
        key_id = new_id("key_")
        label = " ".join((label or "").split())[:80] or f"{spec.name} key"
        with self.db.tx() as conn:
            conn.execute(
                "INSERT INTO provider_keys (id, provider_id, label, secret, key_hint, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    key_id,
                    provider_id,
                    label,
                    self.keybox.encrypt(key, f"{key_id}:{provider_id}"),
                    "…" + key[-4:],
                    iso(datetime.now(UTC)),
                ),
            )
        if allowance is not None:
            self._set_allowance(key_id, provider_id, allowance, period, expires_at)
            return self.check_key(key_id) if spec.free_key_check else self.get_key(key_id)
        # No amount entered: use the plan size the provider reports, else its documented free tier.
        view = self.check_key(key_id, sync_usage=False) if spec.free_key_check else self.get_key(key_id)
        reported = (view.get("balance") or {}).get("allowance")
        if reported:
            self._set_allowance(key_id, provider_id, int(reported), "monthly", None)
        elif use_default_allowance and spec.free_allowance is not None:
            self._set_allowance(key_id, provider_id, spec.free_allowance[0], spec.free_allowance[1], expires_at)
        if spec.free_key_check and view["status"] == "ok":
            return self.check_key(key_id)  # now that an allowance exists, record what is already used
        return self.get_key(key_id)

    def update_key(
        self,
        key_id: str,
        *,
        label: str | None = None,
        enabled: bool | None = None,
        allowance: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        view = self.get_key(key_id)
        with self.db.tx() as conn:
            if label is not None:
                conn.execute("UPDATE provider_keys SET label = ? WHERE id = ?", (" ".join(label.split())[:80] or view["label"], key_id))
            if enabled is not None:
                conn.execute("UPDATE provider_keys SET enabled = ? WHERE id = ?", (int(enabled), key_id))
        if allowance is not None:
            amount = allowance.get("amount")
            self._set_allowance(
                key_id,
                view["provider_id"],
                None if amount in (None, "") else int(amount),
                allowance.get("period"),
                allowance.get("expires_at") or None,
            )
        return self.get_key(key_id)

    def replace_key_secret(self, key_id: str, key: str) -> dict[str, Any]:
        view = self.get_key(key_id)
        key = (key or "").strip()
        if len(key) < 8 or len(key) > 500 or any(c.isspace() for c in key):
            raise invalid("paste the API key exactly as the provider shows it")
        with self.db.tx() as conn:
            conn.execute(
                "UPDATE provider_keys SET secret = ?, key_hint = ?, status = 'unverified', status_detail = NULL WHERE id = ?",
                (self.keybox.encrypt(key, f"{key_id}:{view['provider_id']}"), "…" + key[-4:], key_id),
            )
            reset_scope(conn, ENGINE_FOR_PROVIDER[view["provider_id"]], f"key:{key_id}")
        if SPECS[view["provider_id"]].free_key_check:
            return self.check_key(key_id)
        return self.get_key(key_id)

    def delete_key(self, key_id: str) -> None:
        self.get_key(key_id)
        with self.db.tx() as conn:
            conn.execute("DELETE FROM limits WHERE scope = 'provider_key' AND scope_id = ?", (key_id,))
            conn.execute("DELETE FROM engine_health WHERE scope_key = ?", (f"key:{key_id}",))
            conn.execute("DELETE FROM provider_keys WHERE id = ?", (key_id,))

    def decrypt_provider_key(self, key_id: str) -> str:
        row = self.db.read().execute("SELECT provider_id, secret FROM provider_keys WHERE id = ?", (key_id,)).fetchone()
        if row is None:
            raise GatewayError(ErrorCode.PROVIDER_AUTH_FAILED, "this provider key was deleted")
        try:
            return self.keybox.decrypt(row["secret"], f"{key_id}:{row['provider_id']}")
        except Exception as exc:
            raise GatewayError(
                ErrorCode.PROVIDER_AUTH_FAILED,
                "the stored key cannot be decrypted (was secret.key replaced?); add the key again",
            ) from exc

    def check_key(self, key_id: str, *, sync_usage: bool = True) -> dict[str, Any]:
        """A free call to the provider proving the key works, and reading the balance when it reports one.

        When the provider reports how much of a monthly allowance is already used, this month's local count is
        raised to match (never lowered), so credit spent outside AgentWebView is not spent twice."""
        view = self.get_key(key_id)
        provider_id = view["provider_id"]
        spec = SPECS[provider_id]
        engine = self.engine(ENGINE_FOR_PROVIDER[provider_id])
        now = iso(datetime.now(UTC))
        if not spec.free_key_check:
            with self.db.tx() as conn:
                conn.execute(
                    "UPDATE provider_keys SET status_detail = ?, checked_at = ? WHERE id = ?",
                    (f"{spec.name} has no free key check; the key is verified by its first call", now, key_id),
                )
            return self.get_key(key_id)
        status, detail, balance = "unverified", None, None
        try:
            with ProviderHTTP(self.config, provider_id, spec.hosts) as http:
                reading = engine.check_key(http, self.decrypt_provider_key(key_id))
            status, detail = "ok", "key checked"
            if reading is not None:
                balance = reading.as_dict()
                if reading.remaining is not None:
                    detail = f"key checked; the provider reports {reading.remaining:,} {spec.unit_label} left"
        except KeyCheckFailed as exc:
            if exc.resp.status in (401, 403):
                status, detail = "invalid", f"{spec.name} rejected the key (HTTP {exc.resp.status})"
            else:
                detail = f"the check answered HTTP {exc.resp.status}; the key will be verified by its first call"
        except (ProviderTransportError, GatewayError) as exc:
            detail = f"the check could not reach {spec.name} ({str(exc)[:120]}); the key will be verified by its first call"
        with self.db.tx() as conn:
            conn.execute(
                "UPDATE provider_keys SET status = ?, status_detail = ?, checked_at = ?, balance = COALESCE(?, balance) "
                "WHERE id = ?",
                (status, detail, now, dumps(balance) if balance else None, key_id),
            )
            if status == "ok":
                reset_scope(conn, engine.descriptor.id, f"key:{key_id}")
        if sync_usage and balance and balance.get("used") is not None:
            self._sync_used(key_id, spec.unit, int(balance["used"]))
        return self.get_key(key_id)

    def _sync_used(self, key_id: str, unit: str, reported_used: int) -> None:
        now = datetime.now(UTC)
        tz = self.policy().timezone
        with self.db.tx() as conn:
            for r in conn.execute(
                "SELECT * FROM limits WHERE scope = 'provider_key' AND scope_id = ? AND denomination = ? "
                "AND period = 'monthly'",
                (key_id, unit),
            ).fetchall():
                key, _s, end = limits.period_for("monthly", tz, now)
                conn.execute(
                    "INSERT INTO limit_usage (limit_id, period_key, period_end, used) VALUES (?, ?, ?, ?) "
                    "ON CONFLICT (limit_id, period_key) DO UPDATE SET used = MAX(used, excluded.used)",
                    (r["id"], key, iso(end), max(0, reported_used)),
                )

    def key_options(self, engine_id: str, now: datetime) -> list[AccountOption]:
        engine = self.engine(engine_id)
        provider_id = engine.descriptor.provider_id
        conn = self.db.read()
        tz = self.policy().timezone
        out = []
        for r in conn.execute(
            "SELECT * FROM provider_keys WHERE provider_id = ? AND enabled = 1 ORDER BY label, id", (provider_id,)
        ).fetchall():
            reasons: list[str] = []
            if r["status"] == "invalid":
                reasons.append("the provider rejected this key; replace it or check it again on the Providers screen")
            remaining, soonest = limits.free_state(conn, r["id"], SPECS[provider_id].unit, now, tz)
            out.append(
                AccountOption(
                    key_id=r["id"],
                    label=r["label"],
                    usable=not reasons,
                    reasons=tuple(reasons),
                    expires_at=soonest,
                    remaining=remaining,
                    last_used_at=parse_dt(r["last_used_at"]),
                )
            )
        return out

    # ------------------------------------------------------------------ tasks

    def create_task_row(
        self, *, agent_id: str | None, origin: str, operation: str, summary: str, request: dict[str, Any]
    ) -> str:
        task_id = new_id("tsk_")
        with self.db.tx() as conn:
            conn.execute(
                "INSERT INTO tasks (id, agent_id, origin, operation, summary, request, status, owner_pid, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 'running', ?, ?)",
                (task_id, agent_id, origin, operation, summary[:500], dumps(request), os.getpid(), iso(datetime.now(UTC))),
            )
        return task_id

    def fail_task_row(self, task_id: str, error: GatewayError) -> None:
        result = {"task_id": task_id, "status": "failed", "error": error.to_dict()}
        with self.db.tx() as conn:
            conn.execute(
                "UPDATE tasks SET status = 'failed', error_code = ?, result = ?, completed_at = ? WHERE id = ?",
                (error.code.value, dumps(result), iso(datetime.now(UTC)), task_id),
            )

    def submit(self, data: dict[str, Any], *, agent: dict[str, Any] | None, origin: str) -> dict[str, Any]:
        """Run a search or page read now (returns the receipt), or start a crawl (returns its task id)."""
        spec = parse_task(data)
        agent_id = agent["id"] if agent else None
        if isinstance(spec, SearchTask):
            self.require(agent, "search")
        elif isinstance(spec, ReadPageTask):
            self.require(agent, "read_page")
        else:
            self.require(agent, "crawl")
        policy = self.policy()
        if isinstance(spec, CrawlTask):
            documents.resolve_collection(self.db.read(), spec.input.collection_id)
            for u in [*spec.input.seeds, *spec.input.sitemaps]:
                validate_live_url(policy, u)
        task_id = self.create_task_row(
            agent_id=agent_id,
            origin=origin,
            operation=spec.operation,
            summary=summary_of(spec),
            request=spec.model_dump(mode="json"),
        )
        token = CancelToken()
        with self._lock:
            self._running[task_id] = token
        if isinstance(spec, CrawlTask):
            col = documents.resolve_collection(self.db.read(), spec.input.collection_id)
            with self.db.tx() as conn:
                conn.execute(
                    "INSERT INTO crawls (id, collection_id, agent_id, input, state, created_at) VALUES (?, ?, ?, ?, 'running', ?)",
                    (task_id, col["id"], agent_id, dumps(spec.input.model_dump(mode="json")), iso(datetime.now(UTC))),
                )

            def work() -> None:
                try:
                    self.executor.run(task_id, spec, agent_id, token)
                finally:
                    with self._lock:
                        self._running.pop(task_id, None)

            threading.Thread(target=work, name=f"crawl-{task_id[-8:]}", daemon=True).start()
            return {
                "task_id": task_id,
                "status": "running",
                "operation": "crawl",
                "collection": col["name"],
                "note": "The crawl runs in the background. Check it with task_status, or on the Activity screen.",
            }
        try:
            return self.executor.run(task_id, spec, agent_id, token)
        finally:
            with self._lock:
                self._running.pop(task_id, None)

    def get_task(self, task_id: str, agent_id: str | None = None) -> dict[str, Any]:
        row = self.db.read().execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None or (agent_id is not None and row["agent_id"] != agent_id):
            raise not_found("task")
        if row["status"] != "running" and row["result"]:
            out = loads(row["result"], {})
            out.setdefault("created_at", row["created_at"])
            return out
        view: dict[str, Any] = {
            "task_id": row["id"],
            "status": row["status"],
            "operation": row["operation"],
            "summary": row["summary"],
            "created_at": row["created_at"],
        }
        if row["operation"] == "crawl":
            crawl = self.db.read().execute("SELECT * FROM crawls WHERE id = ?", (task_id,)).fetchone()
            if crawl is not None:
                view["progress"] = loads(crawl["stats"], {})
        if row["operation"] == "browser_session":
            sess = self.db.read().execute("SELECT id FROM browser_sessions WHERE task_id = ?", (task_id,)).fetchone()
            if sess is not None:
                view["session"] = self.browser.get(sess["id"])
        return view

    def wait_task(self, task_id: str, agent_id: str | None, wait_ms: int) -> dict[str, Any]:
        import time

        end = time.monotonic() + max(0, min(wait_ms, 60_000)) / 1000
        while True:
            view = self.get_task(task_id, agent_id)
            if view.get("status") != "running" or time.monotonic() >= end:
                return view
            time.sleep(0.25)

    def cancel_task(self, task_id: str, agent_id: str | None = None) -> dict[str, Any]:
        view = self.get_task(task_id, agent_id)
        token = self._running.get(task_id)
        if token is not None:
            token.cancel("cancelled by request")
        if view.get("operation") == "browser_session" and view.get("status") == "running":
            sess = self.db.read().execute("SELECT id FROM browser_sessions WHERE task_id = ?", (task_id,)).fetchone()
            if sess is not None:
                self.browser.close(sess["id"], agent_id, "cancelled by request")
        return self.get_task(task_id, agent_id)

    def rate_task(
        self, task_id: str, agent_id: str | None, relevance: str, outcome: str = "unknown", notes: str | None = None
    ) -> dict[str, Any]:
        row = self.db.read().execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None or (agent_id is not None and row["agent_id"] != agent_id):
            raise not_found("task")
        if row["status"] != "succeeded" or not row["engine_id"]:
            raise GatewayError(ErrorCode.CONFLICT, "only a finished, successful task can be rated")
        score = {"relevant": 2, "partial": 1, "irrelevant": 0}.get(relevance)
        if score is None or outcome not in ("succeeded", "failed", "unknown"):
            raise invalid("relevance is relevant, partial or irrelevant; outcome is succeeded, failed or unknown")
        with self.db.tx() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO evaluations (task_id, engine_id, provider_key_id, relevance, score, outcome, "
                "notes, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (task_id, row["engine_id"], row["provider_key_id"], relevance, score, outcome, (notes or "")[:2000] or None,
                 iso(datetime.now(UTC))),
            )
        return {"task_id": task_id, "engine": row["engine_id"], "relevance": relevance, "outcome": outcome, "recorded": True}

    def route_preview(self, data: dict[str, Any], agent: dict[str, Any] | None) -> dict[str, Any]:
        spec = parse_task(data)
        policy = self.policy()
        conn = self.db.read()
        now = datetime.now(UTC)
        from agentwebview.providers.adapters import PROVIDER_UNITS

        agent_id = agent["id"] if agent else None
        plan = plan_route(
            spec,
            policy,
            engines=self.engines(),
            health=load_health(conn),
            budget_probe=lambda eid, kid, amounts: limits.probe(
                conn, amounts, agent_id=agent_id, key_id=kid, provider_units=PROVIDER_UNITS, now=now, tz=policy.timezone
            ),
            remaining_ms=spec.limits.deadline_ms,
            now=now,
            account_options=lambda eid: self.key_options(eid, now),
        )
        return {"operation": spec.operation, "note": "Nothing was run or reserved.", **plan.as_dict()}

    def activity(
        self, *, limit: int = 100, agent_id: str | None = None, operation: str | None = None, status: str | None = None,
        before: str | None = None,
    ) -> list[dict[str, Any]]:
        where, params = [], []
        if agent_id:
            where.append("t.agent_id = ?")
            params.append(agent_id)
        if operation:
            where.append("t.operation = ?")
            params.append(operation)
        if status:
            where.append("t.status = ?")
            params.append(status)
        if before:
            where.append("t.created_at < ?")
            params.append(before)
        sql = (
            "SELECT t.id, t.agent_id, a.name AS agent_name, t.origin, t.operation, t.summary, t.status, t.engine_id, "
            "t.provider_key_id, k.label AS key_label, t.error_code, t.usage, t.created_at, t.completed_at, t.duration_ms "
            "FROM tasks t LEFT JOIN agents a ON a.id = t.agent_id LEFT JOIN provider_keys k ON k.id = t.provider_key_id"
            + (" WHERE " + " AND ".join(where) if where else "")
            + " ORDER BY t.created_at DESC LIMIT ?"
        )
        params.append(max(1, min(limit, 500)))
        rows = self.db.read().execute(sql, params).fetchall()
        return [
            {
                "id": r["id"],
                "agent_id": r["agent_id"],
                "agent_name": r["agent_name"] or ("Dashboard" if r["origin"] == "dashboard" else None),
                "origin": r["origin"],
                "operation": r["operation"],
                "summary": r["summary"],
                "status": r["status"],
                "engine": r["engine_id"],
                "key_label": r["key_label"],
                "error_code": r["error_code"],
                "usage": loads(r["usage"], {}),
                "created_at": r["created_at"],
                "completed_at": r["completed_at"],
                "duration_ms": r["duration_ms"],
            }
            for r in rows
        ]

    # ------------------------------------------------------------------ overview

    def overview(self) -> dict[str, Any]:
        conn = self.db.read()
        now = datetime.now(UTC)
        tz = self.policy().timezone
        month_key, month_start, _ = limits.period_for("monthly", tz, now)
        by_status = {
            r["status"]: int(r["n"])
            for r in conn.execute(
                "SELECT status, COUNT(*) AS n FROM tasks WHERE created_at >= ? GROUP BY status", (iso(month_start),)
            ).fetchall()
        }
        by_engine = [
            {"engine": r["engine_id"], "calls": int(r["n"])}
            for r in conn.execute(
                "SELECT engine_id, COUNT(*) AS n FROM tasks WHERE created_at >= ? AND engine_id IS NOT NULL "
                "GROUP BY engine_id ORDER BY n DESC",
                (iso(month_start),),
            ).fetchall()
        ]
        keys = conn.execute("SELECT COUNT(*) AS n FROM provider_keys WHERE enabled = 1").fetchone()["n"]
        agents = conn.execute("SELECT COUNT(*) AS n, SUM(last_used_at IS NOT NULL) AS c FROM agents WHERE revoked_at IS NULL").fetchone()
        docs = conn.execute("SELECT COUNT(*) AS n FROM documents").fetchone()["n"]
        global_limits = [limits.view(conn, r, now, tz) for r in conn.execute("SELECT * FROM limits WHERE scope = 'global' ORDER BY denomination, period").fetchall()]
        return {
            "period": month_key,
            "setup": {
                "provider_keys": int(keys),
                "agents": int(agents["n"] or 0),
                "agents_connected": int(agents["c"] or 0),
                "documents": int(docs),
            },
            "this_month": {"by_status": by_status, "by_engine": by_engine, "total": sum(by_status.values())},
            "global_limits": global_limits,
            "pending_approvals": len(self.browser.pending_decisions()),
            "recent": self.activity(limit=8),
        }

    def set_global_limit(self, unit: str, period: str, amount: int | None) -> list[dict[str, Any]]:
        if unit not in limits.BUILTIN_UNITS:
            raise invalid(f"unknown unit '{unit}'")
        if period not in ("monthly", "daily"):
            raise invalid("global limits are monthly or daily")
        if amount is not None and amount < 0:
            raise invalid("limits cannot be negative")
        limits.set_limit(
            self.db, scope="global", scope_id="", denomination=unit, period=period, amount=amount,
            name=f"{period.capitalize()} {limits.UNIT_LABELS[unit]}",
        )
        conn = self.db.read()
        now = datetime.now(UTC)
        tz = self.policy().timezone
        return [limits.view(conn, r, now, tz) for r in conn.execute("SELECT * FROM limits WHERE scope = 'global' ORDER BY denomination, period").fetchall()]

    # ------------------------------------------------------------------ documents

    def collections(self) -> list[dict[str, Any]]:
        return documents.list_collections(self.db.read())

    def create_collection(self, name: str, description: str | None = None) -> dict[str, Any]:
        return documents.create_collection(self.db, name, description, datetime.now(UTC))

    def ensure_collection(self, ref: str) -> dict[str, Any]:
        try:
            row = documents.resolve_collection(self.db.read(), ref)
            return documents.collection_view(row)
        except GatewayError:
            return self.create_collection(ref)

    def add_document(
        self,
        collection: str,
        filename: str,
        raw: bytes,
        *,
        agent: dict[str, Any] | None = None,
        media_type: str | None = None,
        source_url: str | None = None,
    ) -> dict[str, Any]:
        self.require(agent, "add_documents")
        if len(raw) > documents.MAX_UPLOAD_BYTES:
            raise GatewayError(ErrorCode.PAYLOAD_TOO_LARGE, f"files up to {documents.MAX_UPLOAD_BYTES // 1_000_000} MB")
        col = documents.resolve_collection(self.db.read(), collection)
        try:
            ex = extract(filename, raw, declared_media_type=media_type, original_url=source_url,
                         max_chars=documents.MAX_DOCUMENT_CHARS)
        except RejectedImport as exc:
            raise GatewayError(ErrorCode.CAPABILITY_UNSUPPORTED, str(exc)) from exc
        now = datetime.now(UTC)
        with self.db.tx() as conn:
            result = documents.ingest(
                conn,
                col["id"],
                local_identifier=source_url or filename,
                source_type="agent_document" if agent else "upload",
                extracted=ex,
                original_url=source_url,
                now=now,
            )
            row = conn.execute("SELECT * FROM documents WHERE id = ?", (result["document_id"],)).fetchone()
        return {**documents.document_view(row), "status": result["status"]}
