"""Task execution: cache, plan, reserve, call, check, settle, bounded fallback, finalize.

Invariants
* No database transaction is open while an engine does I/O.
* The call's cost is reserved (every applicable limit at once) before the engine runs.
* Consumption is settled even when evidence fails quality checks or the call is cancelled.
* At most ``max_attempts`` attempts inside one absolute deadline.
* Invalid input, blocked destinations, cancellation and deadline errors never cascade into fallback attempts.
* Every call ends with one record (the receipt): what was asked, which engine and key answered, what it cost,
  and the sources, readable on the Activity screen.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from collections import defaultdict
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from agentwebview import limits
from agentwebview.contract import AnyTask, CrawlTask, coverage_of
from agentwebview.db import dumps, iso, loads, parse_dt
from agentwebview.engine import CancelToken, EngineOutcome, Evidence, ExecutionContext, NetworkClass
from agentwebview.errors import ErrorCode, GatewayError
from agentwebview.policy import Policy
from agentwebview.providers.adapters import PROVIDER_UNITS, SPECS
from agentwebview.routing import (
    AccountOption,
    Candidate,
    RoutePlan,
    load_health,
    plan_route,
    record_outcome,
    required_passed,
    run_checks,
)

log = logging.getLogger("agentwebview.executor")

NO_FALLBACK = frozenset(
    {
        ErrorCode.VALIDATION_FAILED,
        ErrorCode.AUTH_SCOPE_DENIED,
        ErrorCode.CAPABILITY_UNSUPPORTED,
        ErrorCode.NETWORK_BLOCKED,
        ErrorCode.CANCELED,
        ErrorCode.SESSION_STATE_UNKNOWN,
        ErrorCode.DEADLINE_EXCEEDED,
    }
)
COVERAGE_STATEMENTS = {
    "owned_corpus": "Results come only from your own collections. This is not a web search.",
    "stored": "Content was read from a stored copy in your collections; no network request was made.",
    "live_destination": "Content was fetched live from the requested destination only.",
    "web_index": "Results come from a connected provider's web index. Its coverage and freshness are the "
    "provider's; this is not a complete or global web search.",
}
CRAWL_GRACE_MS = 30_000


class _Run:
    def __init__(self, cache_state: str) -> None:
        self.events: list[dict[str, Any]] = []
        self.usage: dict[str, int] = defaultdict(int)
        self.evidence: list[dict[str, Any]] = []
        self.coverage: dict[str, Any] = {}
        self.quality: list[dict[str, Any]] = []
        self.content: str | None = None
        self.engine_id: str | None = None
        self.key_id: str | None = None
        self.success = False
        self.canceled = False
        self.error: GatewayError | None = None
        self.cache: dict[str, Any] = {"state": cache_state}
        self.attempts: list[dict[str, Any]] = []

    def trace(self, step: str, **detail: Any) -> None:
        self.events.append(
            {"seq": len(self.events) + 1, "at": datetime.now(UTC).isoformat(), "step": step, **detail}
        )


class Executor:
    def __init__(self, app: Any, *, clock: Callable[[], datetime] | None = None) -> None:
        self.app = app
        self.clock = clock or (lambda: datetime.now(UTC))

    # ------------------------------------------------------------------ entry point

    def run(self, task_id: str, spec: AnyTask, agent_id: str | None, cancel: CancelToken | None = None) -> dict[str, Any]:
        cancel = cancel or CancelToken()
        policy: Policy = self.app.policy()
        started = time.monotonic()
        now = self.clock()
        cache_mode = spec.policy.cache_mode
        run = _Run("bypass" if cache_mode == "bypass" else "miss")
        budget_ms = spec.limits.deadline_ms
        if isinstance(spec, CrawlTask):
            budget_ms = min(spec.input.max_duration_ms, policy.crawl.max_duration_ms) + CRAWL_GRACE_MS
        deadline_mono = started + budget_ms / 1000
        max_attempts = spec.limits.max_attempts or policy.max_attempts
        tz = policy.timezone

        def remaining_ms() -> int:
            return max(0, int((deadline_mono - time.monotonic()) * 1000))

        try:
            # ---- cache --------------------------------------------------------------------
            cache_key: str | None = None
            if policy.cache.enabled and cache_mode != "bypass" and spec.operation in ("search", "read_page"):
                cache_key = _cache_key(spec)
                if self._try_cache(task_id, spec, agent_id, cache_key, policy, run, now):
                    return self._finalize(task_id, spec, run, started)

            # ---- plan ---------------------------------------------------------------------
            conn = self.app.db.read()
            health = load_health(conn)
            options_cache: dict[str, list[AccountOption]] = {}

            def options_for(engine_id: str) -> list[AccountOption]:
                if engine_id not in options_cache:
                    options_cache[engine_id] = self.app.key_options(engine_id, now)
                return options_cache[engine_id]

            def probe(engine_id: str, key_id: str | None, amounts: dict[str, int]) -> list[limits.Shortfall]:
                return limits.probe(
                    conn, amounts, agent_id=agent_id, key_id=key_id, provider_units=PROVIDER_UNITS, now=now, tz=tz
                )

            plan = plan_route(
                spec,
                policy,
                engines=self.app.engines(),
                health=health,
                budget_probe=probe,
                remaining_ms=remaining_ms(),
                now=now,
                account_options=options_for,
            )
            run.trace("route_planned", **plan.as_dict())
            if not plan.candidates:
                if plan.only_budget_exclusions:
                    run.error = limits.budget_exhausted(plan.budget_shortfalls())
                    run.error.details["exclusions"] = [e.as_dict() for e in plan.exclusions]
                else:
                    run.error = GatewayError(
                        ErrorCode.NO_ELIGIBLE_ENGINE,
                        "no engine can do this under the current settings and limits",
                        details={"exclusions": [e.as_dict() for e in plan.exclusions]},
                        remediation=_remediation(plan),
                    )
                return self._finalize(task_id, spec, run, started)

            # ---- attempts -----------------------------------------------------------------
            attempts_used = 0
            for cand in plan.candidates:
                if attempts_used >= max_attempts:
                    run.trace("attempt_limit_reached", max_attempts=max_attempts)
                    break
                if cancel.cancelled:
                    run.canceled = True
                    run.trace("canceled_between_attempts")
                    break
                engine = self.app.engine(cand.engine_id)
                if remaining_ms() < engine.descriptor.min_deadline_ms:
                    run.error = GatewayError(ErrorCode.DEADLINE_EXCEEDED, "the task's deadline was reached")
                    run.trace("deadline_exceeded", remaining_ms=remaining_ms())
                    break
                try:
                    reservation_id = limits.reserve(
                        self.app.db,
                        task_id=task_id,
                        engine_id=cand.engine_id,
                        amounts=cand.estimate,
                        agent_id=agent_id,
                        key_id=cand.key_id,
                        provider_units=PROVIDER_UNITS,
                        now=self.clock(),
                        tz=tz,
                    )
                except GatewayError as exc:
                    if exc.code != ErrorCode.BUDGET_EXHAUSTED:
                        raise
                    run.error = exc
                    run.trace("reservation_denied", engine=cand.engine_id, error=exc.to_dict())
                    if not plan.fallback_allowed:
                        break
                    continue
                attempts_used += 1
                run.trace(
                    "reserved",
                    attempt=attempts_used,
                    engine=cand.engine_id,
                    amounts={k: str(v) for k, v in cand.estimate.items()},
                    **({"key": {"id": cand.key_id, "label": cand.key_label}} if cand.key_id else {}),
                )
                outcome, latency_ms, extra_used = self._execute(
                    engine, spec, task_id, agent_id, policy, deadline_mono, cancel, cand, tz
                )
                now2 = self.clock()
                quality = run_checks(spec, outcome, now2) if outcome.ok else []
                passed = outcome.ok and required_passed(quality)
                error = outcome.error
                if outcome.ok and not passed:
                    empty = spec.operation == "search" and not outcome.evidence
                    error = GatewayError(
                        ErrorCode.INSUFFICIENT_EVIDENCE if empty else ErrorCode.QUALITY_FAILED,
                        ("nothing matched this query in " + ("your documents" if coverage_of(spec) == "owned_corpus" else "the results"))
                        if empty
                        else "the result failed required quality checks",
                        details={"failed_checks": [c["check"] for c in quality if c["required"] and not c["passed"]]},
                        remediation=(
                            "add documents to a collection, or search the web with scope 'web'"
                            if empty and coverage_of(spec) == "owned_corpus"
                            else None
                        ),
                    )
                settle_usage = dict(outcome.usage)
                if extra_used:  # crawls settle pages one by one; only the base units remain here
                    settle_usage = {k: v for k, v in outcome.usage.items() if k in cand.estimate}
                overrun = limits.settle(self.app.db, reservation_id, settle_usage, now2)
                self._after_attempt(spec, cand, engine, outcome, error, passed, latency_ms, now2, tz)
                for d, v in outcome.usage.items():
                    run.usage[d] += int(v)
                attempt_view = {
                    "attempt": attempts_used,
                    "engine_id": cand.engine_id,
                    "key": {"id": cand.key_id, "label": cand.key_label} if cand.key_id else None,
                    "state": "succeeded" if passed else "failed",
                    "error_code": error.code.value if error else None,
                    "usage": {k: str(v) for k, v in outcome.usage.items()},
                    "quality": quality,
                    "latency_ms": round(latency_ms, 1),
                }
                run.attempts.append(attempt_view)
                self._store_attempt(task_id, attempts_used, cand, attempt_view, now2)
                run.trace(
                    "settled",
                    attempt=attempts_used,
                    engine=cand.engine_id,
                    passed=passed,
                    usage={k: str(v) for k, v in outcome.usage.items()},
                    overrun={k: str(v) for k, v in overrun.items()} or None,
                    error=error.to_dict() if error else None,
                    latency_ms=round(latency_ms, 1),
                )
                if passed:
                    run.success = True
                    run.error = None
                    run.engine_id = cand.engine_id
                    run.key_id = cand.key_id
                    run.evidence = [e.as_dict() for e in outcome.evidence]
                    run.coverage = outcome.coverage
                    run.quality = quality
                    run.content = outcome.content
                    if cache_key is not None:
                        self._cache_store(cache_key, cand.engine_id, run, policy, now2)
                        run.cache = {"state": "miss", "stored": True, "ttl_seconds": policy.cache.ttl_seconds}
                    break
                run.error = error
                run.quality = quality
                run.coverage = outcome.coverage or run.coverage
                if error is not None and error.code == ErrorCode.CANCELED:
                    run.canceled = True
                    break
                if error is not None and error.code in NO_FALLBACK:
                    run.trace("fallback_stopped", reason=f"{error.code} is not eligible for fallback")
                    break
                if not plan.fallback_allowed:
                    run.trace("fallback_stopped", reason="pinned routing disables fallback")
                    break
        except GatewayError as exc:
            run.error = exc
        except Exception as exc:  # never leave a task without a receipt
            log.exception("task %s failed unexpectedly", task_id)
            run.error = GatewayError(ErrorCode.INTERNAL, f"internal error: {type(exc).__name__}")
        return self._finalize(task_id, spec, run, started)

    # ------------------------------------------------------------------ one attempt

    def _execute(
        self,
        engine: Any,
        spec: AnyTask,
        task_id: str,
        agent_id: str | None,
        policy: Policy,
        deadline_mono: float,
        cancel: CancelToken,
        cand: Candidate,
        tz: str,
    ) -> tuple[EngineOutcome, float, bool]:
        app = self.app
        credentials: Callable[[], str] | None = None
        if cand.key_id is not None:
            key_id = cand.key_id

            def credentials() -> str:
                """Decrypt the chosen key here, at dispatch time."""
                return app.decrypt_provider_key(key_id)

        extra: list[dict[str, Any]] = []

        def reserve_more(amounts: dict[str, int]) -> dict[str, Any]:
            rid = limits.reserve(
                app.db,
                task_id=task_id,
                engine_id=cand.engine_id,
                amounts=amounts,
                agent_id=agent_id,
                key_id=None,
                provider_units=PROVIDER_UNITS,
                now=self.clock(),
                tz=tz,
            )
            handle = {"reservation_id": rid, "amounts": dict(amounts), "settled": False}
            extra.append(handle)
            return handle

        def settle_more(handle: dict[str, Any], actual: dict[str, int]) -> None:
            limits.settle(app.db, handle["reservation_id"], actual, self.clock())
            handle["settled"] = True

        ctx = ExecutionContext(
            task=spec,
            task_id=task_id,
            agent_id=agent_id,
            policy=policy,
            deadline_monotonic=deadline_mono,
            cancel=cancel,
            reserved=dict(cand.estimate),
            app=app,
            reserve_more=reserve_more,
            settle_more=settle_more,
            provider_key_id=cand.key_id,
            credentials=credentials,
        )
        t0 = time.monotonic()
        try:
            outcome = engine.execute(ctx)
        except GatewayError as exc:
            outcome = EngineOutcome(ok=False, error=exc, usage=dict(cand.estimate))
        except (ConnectionError, TimeoutError, OSError) as exc:
            outcome = EngineOutcome(
                ok=False,
                transport_error=True,
                usage=dict(cand.estimate),
                error=GatewayError(ErrorCode.ENGINE_FAILURE, f"transport error: {type(exc).__name__}", retryable=True),
            )
        except Exception as exc:
            log.exception("engine %s failed", engine.descriptor.id)
            outcome = EngineOutcome(
                ok=False,
                usage=dict(cand.estimate),
                error=GatewayError(ErrorCode.ENGINE_FAILURE, f"engine error: {type(exc).__name__}"),
            )
        for h in extra:
            if not h["settled"]:  # the engine stopped between reserving and settling: charge what was held
                limits.settle(app.db, h["reservation_id"], h["amounts"], self.clock())
        return outcome, (time.monotonic() - t0) * 1000, bool(extra)

    def _after_attempt(
        self,
        spec: AnyTask,
        cand: Candidate,
        engine: Any,
        outcome: EngineOutcome,
        error: GatewayError | None,
        passed: bool,
        latency_ms: float,
        now: datetime,
        tz: str,
    ) -> None:
        """Health, the key's status, and the local allowance after a provider said its credit ran out."""
        rl_until = datetime.fromtimestamp(outcome.rate_limited_until, UTC) if outcome.rate_limited_until else None
        dest = outcome.coverage.get("host") if isinstance(outcome.coverage, dict) else None
        host_scoped = engine.descriptor.network_class == NetworkClass.DIRECT_WEB and bool(dest)
        key_scoped = cand.key_id is not None
        auth_failure = bool(outcome.detail.get("auth_failure"))
        with self.app.db.tx() as conn:
            record_outcome(
                conn,
                cand.engine_id,
                now=now,
                success=passed,
                transport_error=outcome.transport_error and not host_scoped,
                quality_passed=passed if outcome.ok else None,
                latency_ms=latency_ms,
                error_code=error.code.value if error else None,
                # Provider rate limits apply to one key, destination limits to one host: cool down only that.
                rate_limited_until=None if host_scoped or key_scoped else rl_until,
            )
            if host_scoped:
                record_outcome(
                    conn,
                    cand.engine_id,
                    now=now,
                    success=passed,
                    transport_error=outcome.transport_error,
                    quality_passed=None,
                    latency_ms=None,
                    error_code=error.code.value if error else None,
                    rate_limited_until=rl_until,
                    scope_key=str(dest),
                )
            if key_scoped:
                record_outcome(
                    conn,
                    cand.engine_id,
                    now=now,
                    success=passed,
                    transport_error=False,  # an outage counts against the provider (above), not this key
                    quality_passed=passed if outcome.ok else None,
                    latency_ms=latency_ms,
                    error_code=error.code.value if error else None,
                    rate_limited_until=rl_until,
                    scope_key=f"key:{cand.key_id}",
                    auth_failure=auth_failure,
                )
                conn.execute(
                    "UPDATE provider_keys SET last_used_at = ?, "
                    "status = CASE WHEN ? THEN 'invalid' WHEN ? AND status = 'unverified' THEN 'ok' ELSE status END, "
                    "status_detail = CASE WHEN ? THEN ? ELSE status_detail END WHERE id = ?",
                    (
                        iso(now),
                        auth_failure,
                        outcome.ok,
                        auth_failure,
                        (error.message if error else "the provider rejected the key")[:300],
                        cand.key_id,
                    ),
                )
        if key_scoped and outcome.detail.get("credits_exhausted"):
            spec_ = SPECS.get(engine.descriptor.provider_id or "")
            if spec_ is not None and cand.key_id is not None:
                limits.write_off(self.app.db, cand.key_id, spec_.unit, now, tz)

    def _store_attempt(
        self, task_id: str, attempt_no: int, cand: Candidate, view: dict[str, Any], now: datetime
    ) -> None:
        with self.app.db.tx() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO attempts (task_id, attempt_no, engine_id, provider_key_id, state, error_code, "
                "usage, quality, latency_ms, started_at, finished_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    task_id,
                    attempt_no,
                    cand.engine_id,
                    cand.key_id,
                    view["state"],
                    view["error_code"],
                    dumps(view["usage"]),
                    dumps(view["quality"]),
                    view["latency_ms"],
                    iso(now - timedelta(milliseconds=view["latency_ms"])),
                    iso(now),
                ),
            )

    # ------------------------------------------------------------------ cache

    def _try_cache(
        self, task_id: str, spec: AnyTask, agent_id: str | None, key: str, policy: Policy, run: _Run, now: datetime
    ) -> bool:
        """Serve a valid cached result (charged one request). Returns True when the task is finished."""
        mode = spec.policy.cache_mode
        row = self.app.db.read().execute("SELECT * FROM cache_entries WHERE key = ?", (key,)).fetchone()
        cached = None
        state = "miss"
        if row is not None:
            created = parse_dt(row["created_at"]) or now
            fresh = (parse_dt(row["expires_at"]) or now) > now
            stale_ok = mode in ("use_cache", "cache_only") and (parse_dt(row["stale_until"]) or now) > now
            max_age = spec.requirements.max_age_seconds
            young = max_age is None or (now - created).total_seconds() <= max_age
            if (fresh or stale_ok) and young:
                cached = loads(row["response"], {})
                state = "hit" if fresh else "stale_hit"
        if cached is not None:
            outcome = EngineOutcome(
                ok=True,
                evidence=[Evidence(**e) for e in cached.get("evidence", [])],
                coverage=cached.get("coverage", {}),
                content=cached.get("content"),
            )
            checks = run_checks(spec, outcome, now)
            if not required_passed(checks):
                run.trace("cache_entry_rejected", reason="cached evidence no longer passes required checks")
                cached = None
        if cached is None:
            run.trace("cache_miss", key=key[:16], mode=mode)
            if mode == "cache_only":
                run.error = GatewayError(
                    ErrorCode.NO_ELIGIBLE_ENGINE,
                    "cache_only was requested and no valid cached result exists",
                    remediation="use cache_mode prefer_fresh",
                )
                return True
            return False
        amounts = {"task_calls": 1}
        try:
            rid = limits.reserve(
                self.app.db,
                task_id=task_id,
                engine_id="cache",
                amounts=amounts,
                agent_id=agent_id,
                key_id=None,
                provider_units=PROVIDER_UNITS,
                now=now,
                tz=policy.timezone,
            )
        except GatewayError as exc:
            if exc.code != ErrorCode.BUDGET_EXHAUSTED:
                raise
            run.error = exc
            run.trace("reservation_denied", engine="cache", error=exc.to_dict())
            return True
        limits.settle(self.app.db, rid, amounts, now)
        with self.app.db.tx() as conn:
            conn.execute("UPDATE cache_entries SET hits = hits + 1 WHERE key = ?", (key,))
        stored_at = parse_dt(row["created_at"]) if row is not None else None
        run.trace("cache_hit", state=state, key=key[:16], original_engine=cached.get("engine_id"))
        run.success = True
        run.engine_id = cached.get("engine_id")
        run.evidence = cached.get("evidence", [])
        run.coverage = cached.get("coverage", {})
        run.content = cached.get("content")
        run.quality = checks
        run.usage["task_calls"] += 1
        run.cache = {
            "state": state,
            "key": key[:16],
            "stored_at": iso(stored_at),
            "age_seconds": round((now - stored_at).total_seconds(), 3) if stored_at else None,
            "charged": {"task_calls": "1"},
            "note": "served from the local result cache; evidence timestamps are the original retrieval times",
        }
        return True

    def _cache_store(self, key: str, engine_id: str, run: _Run, policy: Policy, now: datetime) -> None:
        body = {
            "engine_id": engine_id,
            "evidence": run.evidence,
            "coverage": run.coverage,
            "content": run.content,
        }
        with self.app.db.tx() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO cache_entries (key, response, created_at, expires_at, stale_until, hits) "
                "VALUES (?, ?, ?, ?, ?, 0)",
                (
                    key,
                    dumps(body),
                    iso(now),
                    iso(now + timedelta(seconds=policy.cache.ttl_seconds)),
                    iso(now + timedelta(seconds=policy.cache.ttl_seconds + policy.cache.stale_seconds)),
                ),
            )

    # ------------------------------------------------------------------ finalize

    def _finalize(self, task_id: str, spec: AnyTask, run: _Run, started: float) -> dict[str, Any]:
        now = self.clock()
        if run.success:
            status, error = "succeeded", None
        elif run.canceled:
            status = "canceled"
            error = run.error if run.error and run.error.code == ErrorCode.CANCELED else GatewayError(
                ErrorCode.CANCELED, "the task was cancelled; what was already used stays charged"
            )
        else:
            status = "failed"
            error = run.error or GatewayError(ErrorCode.NO_ELIGIBLE_ENGINE, "no attempt succeeded")
        run.trace("finalized", status=status, error_code=error.code.value if error else None)
        coverage = dict(run.coverage)
        kind = coverage.get("kind") or coverage_of(spec)
        coverage.setdefault("kind", kind)
        coverage.setdefault("statement", COVERAGE_STATEMENTS.get(kind, ""))
        duration_ms = int((time.monotonic() - started) * 1000)
        result: dict[str, Any] = {
            "task_id": task_id,
            "status": status,
            "operation": spec.operation,
            "engine": run.engine_id,
            "provider_key": run.key_id,
            "evidence": run.evidence,
            "coverage": coverage,
            "source_freshness": _freshness(run.evidence),
            "cache": run.cache,
            "quality_checks": run.quality,
            "quality_note": "Quality checks are deterministic proxies; they do not establish factual truth.",
            "resource_usage": {k: str(v) for k, v in sorted(run.usage.items())},
            "attempts": run.attempts,
            "error": error.to_dict() if error else None,
            "completed_at": iso(now),
            "duration_ms": duration_ms,
            "trace": run.events,
            "retrieved_content_is_data": True,
        }
        if run.content is not None:
            result["content"] = run.content
        with self.app.db.tx() as conn:
            conn.execute(
                "UPDATE tasks SET status = ?, engine_id = ?, provider_key_id = ?, error_code = ?, usage = ?, result = ?, "
                "trace = ?, completed_at = ?, duration_ms = ? WHERE id = ?",
                (
                    status,
                    run.engine_id,
                    run.key_id,
                    error.code.value if error else None,
                    dumps(dict(run.usage)),
                    dumps(result),
                    dumps(run.events),
                    iso(now),
                    duration_ms,
                    task_id,
                ),
            )
            row = conn.execute("SELECT created_at FROM tasks WHERE id = ?", (task_id,)).fetchone()
        result["created_at"] = row["created_at"] if row else None
        return result


def _cache_key(spec: AnyTask) -> str:
    material = {
        "operation": spec.operation,
        "input": spec.input.model_dump(mode="json"),
        "coverage": coverage_of(spec),
        "max_results": spec.limits.max_results,
        "max_excerpt_chars": spec.limits.max_excerpt_chars,
        "routing": spec.policy.pinned_engine,
        "allowed": spec.policy.allowed_engines,
    }
    blob = json.dumps(material, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _freshness(evidence: list[dict[str, Any]]) -> dict[str, Any]:
    def pick(field: str, fn: Any) -> str | None:
        vals = [e[field] for e in evidence if e.get(field)]
        return fn(vals) if vals else None

    return {
        "oldest_fetched_at": pick("fetched_at", min),
        "newest_fetched_at": pick("fetched_at", max),
        "oldest_indexed_at": pick("indexed_at", min),
        "newest_indexed_at": pick("indexed_at", max),
        "note": "Retrieval timestamps (fetched/indexed/imported) are distinct from optional publication time.",
    }


def _remediation(plan: RoutePlan) -> str:
    kinds = {k for e in plan.exclusions for k in e.kinds}
    if "budget" in kinds:
        return "wait for the next period, raise the limit, or add credit to a provider key"
    if "account" in kinds and plan.coverage == "web_index":
        return "add a provider key on the Providers screen (Exa, Tavily, Firecrawl, SerpApi and others)"
    if "network" in kinds and plan.coverage == "web_index":
        return "turn web search providers on in Settings"
    if "network" in kinds:
        return "turn on reading websites directly in Settings, or add a Firecrawl or Jina key"
    if "coverage" in kinds and plan.coverage == "owned_corpus":
        return "search covers your own documents by default; add documents, or ask with scope 'web'"
    return "read the exclusion reasons in the trace and adjust the settings or the request"
