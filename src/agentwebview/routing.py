"""Deterministic routing, engine health and evidence quality checks. No model or LLM is consulted.

1. Filter: every engine is checked against operation, the caller's allow-list and pin, which tools are on
   (providers, direct web reading), coverage, required capabilities, engine-specific requirements, health
   (circuit breaker / rate-limit cooldown), remaining deadline, quality floor and limits. Every exclusion
   carries its reasons. A provider engine is expanded into one candidate per connected key that can be used;
   keys that cannot be used are excluded with their reasons.
2. Score: a stable weighted sum over min-max normalised cost, latency and observed quality (unknown quality
   is explicit and uses a neutral 0.5 prior; labelled evaluations replace the proxy pass rate once there are
   enough of them). Ties break by engine id and key.
3. Expiry preference applies only among candidates whose scores are within ``EXPIRY_EPSILON`` of the best,
   never to promote an engine that fails a hard requirement. Among those, credit that expires soonest is
   spent first, then the key with the most credit left.
4. Search rotation ("round_robin"): for web searches, the provider keys that passed every filter are put in
   least-recently-used order, so consecutive searches spread across every provider with credit left. The rest
   of the order is the fallback chain: a key whose provider reports its credits exhausted is written off for
   the period and the next one takes over.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from agentwebview.contract import AnyTask, coverage_of
from agentwebview.db import iso, parse_dt
from agentwebview.engine import EngineDescriptor, EngineOutcome, NetworkClass
from agentwebview.limits import Shortfall
from agentwebview.policy import Policy

COST_WEIGHTS: dict[str, float] = {
    "task_calls": 1.0,
    "search_calls": 1.0,
    "fetched_pages": 5.0,
    "browser_ms": 1 / 100,
}
MODE_WEIGHTS: dict[str, tuple[float, float, float]] = {  # (cost, latency, quality)
    "economical": (0.6, 0.2, 0.2),
    "balanced": (0.34, 0.33, 0.33),
    "quality": (0.2, 0.1, 0.7),
    "pinned": (0.34, 0.33, 0.33),
}
# Provider candidates: a fixed cost for calling out (so built-in engines win ties in economical mode), plus the
# share of the key's remaining credit the call would use, scaled. Units of different providers (USD
# micro-units, credits, tokens) are never compared directly; their share of what is left is.
PROVIDER_CALL_COST = 100.0
PROVIDER_SHARE_COST = 100.0
EXPIRY_EPSILON = 0.02
MIN_QUALITY_SAMPLES = 5
MIN_LABELS = 5  # labelled evaluations needed before they replace the proxy pass rate

BREAKER_THRESHOLD = 3
BREAKER_OPEN_SECONDS = 30
# A key the provider rejected (401/403) stays out of routing until it is replaced or checked again.
AUTH_FAILURE_OPEN_SECONDS = 24 * 3600
EWMA_ALPHA = 0.2


# =========================================================================== health


@dataclass(frozen=True)
class HealthView:
    circuit_state: str = "CLOSED"
    open_until: datetime | None = None
    cooldown_until: datetime | None = None
    latency_ewma_ms: float | None = None
    quality_passes: int = 0
    quality_checks: int = 0
    label_score_sum: int = 0
    labels: int = 0

    @property
    def quality(self) -> float | None:
        if self.labels >= MIN_LABELS:
            return self.label_score_sum / (2 * self.labels)
        if self.quality_checks < MIN_QUALITY_SAMPLES:
            return None
        return self.quality_passes / self.quality_checks

    @property
    def quality_basis(self) -> str:
        if self.labels >= MIN_LABELS:
            return "labelled"
        return "proxy" if self.quality_checks >= MIN_QUALITY_SAMPLES else "unknown"


def load_health(conn: Any) -> dict[str, HealthView]:
    """Health keyed by engine id; scoped health keyed ``engine@<scope>`` (a host or ``key:<id>``)."""
    labels: dict[str, tuple[int, int]] = {}
    for r in conn.execute(
        "SELECT engine_id, provider_key_id, COALESCE(SUM(score), 0) AS total, COUNT(*) AS n "
        "FROM evaluations WHERE engine_id IS NOT NULL GROUP BY engine_id, provider_key_id"
    ).fetchall():
        keys = [str(r["engine_id"])] + ([f"{r['engine_id']}@key:{r['provider_key_id']}"] if r["provider_key_id"] else [])
        for k in keys:
            prev = labels.get(k, (0, 0))
            labels[k] = (prev[0] + int(r["total"]), prev[1] + int(r["n"]))
    out: dict[str, HealthView] = {}
    for r in conn.execute("SELECT * FROM engine_health").fetchall():
        key = r["engine_id"] if not r["scope_key"] else f"{r['engine_id']}@{r['scope_key']}"
        lab = labels.pop(key, (0, 0))
        out[key] = HealthView(
            r["circuit_state"],
            parse_dt(r["open_until"]),
            parse_dt(r["cooldown_until"]),
            r["latency_ewma_ms"],
            int(r["quality_passes"]),
            int(r["quality_checks"]),
            label_score_sum=lab[0],
            labels=lab[1],
        )
    for key, lab in labels.items():
        out[key] = HealthView(label_score_sum=lab[0], labels=lab[1])
    return out


def record_outcome(
    conn: Any,
    engine_id: str,
    *,
    now: datetime,
    success: bool,
    transport_error: bool,
    quality_passed: bool | None,
    latency_ms: float | None,
    error_code: str | None,
    rate_limited_until: datetime | None = None,
    scope_key: str = "",
    auth_failure: bool = False,
) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO engine_health (engine_id, scope_key) VALUES (?, ?)", (engine_id, scope_key)
    )
    h = dict(
        conn.execute(
            "SELECT * FROM engine_health WHERE engine_id = ? AND scope_key = ?", (engine_id, scope_key)
        ).fetchone()
    )
    if auth_failure:
        h["failures"] += 1
        h["last_failure_at"] = iso(now)
        h["circuit_state"] = "OPEN"
        h["open_until"] = iso(now + timedelta(seconds=AUTH_FAILURE_OPEN_SECONDS))
    elif transport_error:
        h["consecutive_transport_errors"] += 1
        h["failures"] += 1
        h["last_failure_at"] = iso(now)
        if h["consecutive_transport_errors"] >= BREAKER_THRESHOLD:
            h["circuit_state"] = "OPEN"
            h["open_until"] = iso(now + timedelta(seconds=BREAKER_OPEN_SECONDS))
    else:
        h["consecutive_transport_errors"] = 0
        if success:
            h["successes"] += 1
            h["last_success_at"] = iso(now)
            h["circuit_state"] = "CLOSED"
            h["open_until"] = None
        else:
            h["failures"] += 1
            h["last_failure_at"] = iso(now)
    if rate_limited_until is not None:
        current = parse_dt(h["cooldown_until"])
        h["cooldown_until"] = iso(max(rate_limited_until, current or rate_limited_until))
    if latency_ms is not None:
        prev = h["latency_ewma_ms"]
        h["latency_ewma_ms"] = latency_ms if prev is None else EWMA_ALPHA * latency_ms + (1 - EWMA_ALPHA) * prev
    if quality_passed is not None:
        h["quality_checks"] += 1
        h["quality_passes"] += int(quality_passed)
    h["last_error_code"] = error_code
    h["updated_at"] = iso(now)
    conn.execute(
        "UPDATE engine_health SET circuit_state = ?, open_until = ?, cooldown_until = ?, "
        "consecutive_transport_errors = ?, successes = ?, failures = ?, latency_ewma_ms = ?, quality_passes = ?, "
        "quality_checks = ?, last_error_code = ?, last_success_at = ?, last_failure_at = ?, updated_at = ? "
        "WHERE engine_id = ? AND scope_key = ?",
        (
            h["circuit_state"],
            h["open_until"],
            h["cooldown_until"],
            h["consecutive_transport_errors"],
            h["successes"],
            h["failures"],
            h["latency_ewma_ms"],
            h["quality_passes"],
            h["quality_checks"],
            h["last_error_code"],
            h["last_success_at"],
            h["last_failure_at"],
            h["updated_at"],
            engine_id,
            scope_key,
        ),
    )


def reset_scope(conn: Any, engine_id: str, scope_key: str) -> None:
    """Close a scope's circuit (a replaced or re-checked key gets a fresh start)."""
    conn.execute(
        "UPDATE engine_health SET circuit_state = 'CLOSED', open_until = NULL, cooldown_until = NULL, "
        "consecutive_transport_errors = 0 WHERE engine_id = ? AND scope_key = ?",
        (engine_id, scope_key),
    )


# =========================================================================== routing


@dataclass(frozen=True)
class AccountOption:
    """One connected provider key an engine could use."""

    key_id: str
    label: str
    usable: bool
    reasons: tuple[str, ...] = ()
    expires_at: datetime | None = None  # soonest end of the credit that would be spent
    remaining: int | None = None  # credit left in the provider's unit (None = unknown)
    last_used_at: datetime | None = None


AccountOptions = Callable[[str], list[AccountOption]]
BudgetProbe = Callable[[str, str | None, dict[str, int]], list[Shortfall]]


@dataclass
class Candidate:
    engine_id: str
    estimate: dict[str, int]
    score: float = 0.0
    features: dict[str, Any] = field(default_factory=dict)
    expiry_rank: float = float("inf")
    key_id: str | None = None
    key_label: str | None = None
    remaining: int | None = None
    last_used_at: datetime | None = None

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "engine_id": self.engine_id,
            "estimate": {k: str(v) for k, v in self.estimate.items()},
            "score": round(self.score, 6),
            "features": self.features,
        }
        if self.key_id is not None:
            out["key"] = {
                "id": self.key_id,
                "label": self.key_label,
                "credit_remaining": None if self.remaining is None else str(self.remaining),
                "expiry_rank": None if self.expiry_rank == float("inf") else self.expiry_rank,
                "last_used_at": iso(self.last_used_at),
            }
        return out


@dataclass
class Exclusion:
    engine_id: str
    reasons: list[str]
    kinds: list[str]
    shortfalls: list[Shortfall] = field(default_factory=list)
    key_id: str | None = None
    key_label: str | None = None

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"engine_id": self.engine_id, "reasons": self.reasons, "kinds": self.kinds}
        if self.key_id is not None:
            out["key"] = {"id": self.key_id, "label": self.key_label}
        if self.shortfalls:
            out["shortfalls"] = [sf.as_dict() for sf in self.shortfalls]
        return out


@dataclass
class RoutePlan:
    mode: str
    candidates: list[Candidate]
    exclusions: list[Exclusion]
    fallback_allowed: bool
    coverage: str
    rotation: str = "score"

    def as_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "rotation": self.rotation,
            "coverage": self.coverage,
            "fallback_allowed": self.fallback_allowed,
            "candidates": [c.as_dict() for c in self.candidates],
            "exclusions": [e.as_dict() for e in self.exclusions],
        }

    def budget_shortfalls(self) -> list[Shortfall]:
        return [sf for e in self.exclusions for sf in e.shortfalls]

    @property
    def only_budget_exclusions(self) -> bool:
        relevant = [
            e
            for e in self.exclusions
            if not {"operation", "coverage", "network", "account"} & set(e.kinds)
        ]
        return bool(relevant) and all(set(e.kinds) <= {"budget"} for e in relevant)


def plan_route(
    task: AnyTask,
    policy: Policy,
    *,
    engines: list[Any],
    health: dict[str, HealthView],
    budget_probe: BudgetProbe | None,
    remaining_ms: int,
    now: datetime | None = None,
    account_options: AccountOptions | None = None,
) -> RoutePlan:
    now = now or datetime.now(UTC)
    coverage = coverage_of(task)
    pinned = task.policy.pinned_engine
    caller_allowed = set(task.policy.allowed_engines) if task.policy.allowed_engines is not None else None
    required_caps = set(task.requirements.capabilities)
    candidates: list[Candidate] = []
    exclusions: list[Exclusion] = []
    dest_host = _destination_host(task)

    for engine in engines:
        d: EngineDescriptor = engine.descriptor
        reasons: list[str] = []
        kinds: list[str] = []

        def no(kind: str, reason: str, kinds: list[str] = kinds, reasons: list[str] = reasons) -> None:
            kinds.append(kind)
            reasons.append(reason)

        if d.network_class == NetworkClass.COMMERCIAL_PROVIDER and not policy.providers_enabled:
            no("network", "web search providers are turned off in Settings")
        if d.network_class == NetworkClass.DIRECT_WEB and not policy.direct_web.enabled:
            no("network", "reading websites directly is turned off in Settings")
        if task.operation not in d.operations:
            no("operation", f"does not support operation '{task.operation}'")
        if caller_allowed is not None and d.id not in caller_allowed:
            no("policy", "not in the caller's allowed engines")
        if pinned and d.id != pinned:
            no("pinned", f"routing is pinned to '{pinned}'")
        if coverage not in d.coverage:
            no("coverage", f"cannot provide coverage '{coverage}' (offers {', '.join(sorted(d.coverage))})")
        missing = required_caps - set(d.capabilities)
        if missing:
            no("capability", f"missing required capabilities: {', '.join(sorted(missing))}")
        if not reasons:
            for r in engine.check_requirements(task, policy):
                no("requirement", r)
        h = health.get(d.id, HealthView())
        scoped = [("engine", h)]
        if dest_host and d.network_class == NetworkClass.DIRECT_WEB:
            scoped.append((dest_host, health.get(f"{d.id}@{dest_host}", HealthView())))
        for scope_name, hv in scoped:
            label = "" if scope_name == "engine" else f" for {scope_name}"
            if hv.circuit_state == "OPEN" and hv.open_until and hv.open_until > now:
                no("health", f"circuit breaker open{label} until {hv.open_until.isoformat()}")
            if hv.cooldown_until and hv.cooldown_until > now:
                no("health", f"rate-limit cooldown{label} until {hv.cooldown_until.isoformat()}")
        if remaining_ms < d.min_deadline_ms:
            no("deadline", f"remaining deadline {remaining_ms} ms is below the engine minimum {d.min_deadline_ms} ms")
        min_q = task.requirements.min_quality
        if min_q is not None and h.quality is not None and h.quality < min_q:
            no("quality", f"observed quality {h.quality:.2f} ({h.quality_basis}) is below the floor {min_q:.2f}")
        if reasons:
            exclusions.append(Exclusion(d.id, reasons, kinds))
            continue
        estimate: dict[str, int] = engine.estimate_resources(task, policy)
        probe_fn = getattr(engine, "probe_resources", None)
        probe_amounts = probe_fn(task, policy) if probe_fn is not None else estimate
        hint = getattr(engine, "cost_hint", None)
        cost_basis = hint(task, policy) if hint is not None else estimate
        provider_unit = d.price_denomination if d.network_class == NetworkClass.COMMERCIAL_PROVIDER else None
        cost_units = round(sum(COST_WEIGHTS.get(k, 1.0) * v for k, v in cost_basis.items() if k != provider_unit), 6)

        options: list[AccountOption | None]
        if d.network_class == NetworkClass.COMMERCIAL_PROVIDER:
            opts = account_options(d.id) if account_options is not None else []
            if not opts:
                exclusions.append(Exclusion(d.id, [f"no {d.name} key added (Providers screen)"], ["account"]))
                continue
            options = list(opts)
        else:
            options = [None]
        for opt in options:
            slot_reasons: list[str] = []
            slot_kinds: list[str] = []
            if opt is not None and not opt.usable:
                slot_reasons.extend(opt.reasons or ("key cannot be used",))
                slot_kinds.extend("account" for _ in (opt.reasons or ("x",)))
            hv = h
            if opt is not None:
                hv = health.get(f"{d.id}@key:{opt.key_id}", HealthView())
                for label, until in (("circuit breaker open", hv.open_until), ("rate-limit cooldown", hv.cooldown_until)):
                    if until and until > now and (label != "circuit breaker open" or hv.circuit_state == "OPEN"):
                        slot_kinds.append("health")
                        slot_reasons.append(f"{label} for this key until {until.isoformat()}")
            shortfalls: list[Shortfall] = []
            if not slot_reasons and budget_probe is not None:
                shortfalls = budget_probe(d.id, opt.key_id if opt else None, probe_amounts)
                for sf in shortfalls:
                    slot_kinds.append("budget")
                    slot_reasons.append(sf.sentence())
            if slot_reasons:
                exclusions.append(
                    Exclusion(
                        d.id,
                        slot_reasons,
                        slot_kinds,
                        shortfalls,
                        key_id=opt.key_id if opt else None,
                        key_label=opt.label if opt else None,
                    )
                )
                continue
            c = Candidate(d.id, estimate)
            quality = hv.quality if opt is not None and hv.quality is not None else h.quality
            c.features = {
                "cost_units": cost_units
                if opt is None
                else round(
                    cost_units
                    + PROVIDER_CALL_COST
                    + PROVIDER_SHARE_COST * min(1.0, cost_basis.get(provider_unit or "", 0) / max(1, opt.remaining or 0)),
                    6,
                ),
                "latency_ms": h.latency_ewma_ms if h.latency_ewma_ms is not None else d.default_latency_ms,
                "latency_observed": h.latency_ewma_ms is not None,
                "quality": quality,
                "quality_known": quality is not None,
                "quality_basis": hv.quality_basis if opt is not None and hv.quality is not None else h.quality_basis,
                "network_class": d.network_class.value,
            }
            if opt is not None:
                c.key_id = opt.key_id
                c.key_label = opt.label
                c.remaining = opt.remaining
                c.last_used_at = opt.last_used_at
                if opt.expires_at is not None:
                    c.expiry_rank = opt.expires_at.timestamp()
            candidates.append(c)

    mode = "pinned" if pinned else (task.policy.routing_mode or policy.routing_mode)
    _score(candidates, mode)
    best = max((c.score for c in candidates), default=0.0)
    candidates.sort(
        key=lambda c: (
            -round(c.score, 6) if best - c.score > EXPIRY_EPSILON else -best,
            c.expiry_rank,
            -(c.remaining if c.remaining is not None else -1),
            -c.score,
            c.engine_id,
            str(c.key_id or ""),
        )
    )
    _order_keys_within_engines(candidates)
    rotation = "round_robin" if policy.search_rotation == "round_robin" and task.operation == "search" else "score"
    if rotation == "round_robin" and mode != "pinned":
        _rotate_provider_slots(candidates)
    if mode == "pinned":
        candidates = candidates[:1]
    return RoutePlan(
        mode=mode,
        candidates=candidates,
        exclusions=exclusions,
        fallback_allowed=mode != "pinned",
        coverage=coverage,
        rotation=rotation if mode != "pinned" else "score",
    )


_NEVER_USED = datetime.min.replace(tzinfo=UTC)


def _rotate_provider_slots(candidates: list[Candidate]) -> None:
    """Round robin across provider keys: built-in engines keep the place scoring gave them, and the provider
    slots are filled least recently used first (never-used keys first), ties broken by engine and key."""
    idxs = [i for i, c in enumerate(candidates) if c.key_id is not None]
    if len(idxs) < 2:
        return
    ordered = sorted(
        (candidates[i] for i in idxs), key=lambda c: (c.last_used_at or _NEVER_USED, c.engine_id, str(c.key_id))
    )
    for i, c in zip(idxs, ordered, strict=True):
        candidates[i] = c


def _order_keys_within_engines(candidates: list[Candidate]) -> None:
    """Keep each engine's place in the ranking, but use its keys in the documented order: the credit that
    expires soonest first, then the key with the most credit left."""
    slots: dict[str, list[int]] = {}
    for i, c in enumerate(candidates):
        if c.key_id is not None:
            slots.setdefault(c.engine_id, []).append(i)
    for idxs in slots.values():
        if len(idxs) < 2:
            continue
        ordered = sorted(
            (candidates[i] for i in idxs),
            key=lambda c: (c.expiry_rank, -(c.remaining if c.remaining is not None else -1), str(c.key_id)),
        )
        for i, c in zip(idxs, ordered, strict=True):
            candidates[i] = c


def _destination_host(task: AnyTask) -> str | None:
    url = getattr(task.input, "url", None)
    if not url:
        return None
    from agentwebview.urls import UrlError, canonicalize, host_of

    try:
        return host_of(canonicalize(url))
    except UrlError:
        return None


def _norm(values: list[float]) -> list[float]:
    lo, hi = min(values), max(values)
    if hi - lo <= 1e-12:
        return [0.0 for _ in values]
    return [(v - lo) / (hi - lo) for v in values]


def _score(candidates: list[Candidate], mode: str) -> None:
    if not candidates:
        return
    wc, wl, wq = MODE_WEIGHTS[mode]
    cost = _norm([float(c.features["cost_units"]) for c in candidates])
    lat = _norm([float(c.features["latency_ms"]) for c in candidates])
    for c, nc, nl in zip(candidates, cost, lat, strict=True):
        q = c.features["quality"] if c.features["quality"] is not None else 0.5
        c.score = wc * (1 - nc) + wl * (1 - nl) + wq * q
        c.features["normalized"] = {"cost": round(nc, 6), "latency": round(nl, 6), "quality": round(q, 6)}
        c.features["weights"] = {"cost": wc, "latency": wl, "quality": wq}


# =========================================================================== quality checks
# Cheap deterministic proxies (emptiness, navigation-only text, provenance, duplicates, freshness, simple
# lexical relevance). They cannot establish that a claim is true and are reported as such.

_WORD = re.compile(r"[\w']{3,}", re.UNICODE)
_NAV_WORDS = {
    "home", "about", "contact", "login", "sign", "menu", "privacy", "terms", "cookies", "search", "subscribe",
    "register", "help", "careers", "blog", "news", "faq", "cart", "account",
}  # fmt: skip


def _check(name: str, passed: bool, required: bool, detail: str) -> dict[str, Any]:
    return {"check": name, "passed": passed, "required": required, "detail": detail}


def navigation_only(text: str) -> bool:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    words = _WORD.findall(text.lower())
    if len(words) < 25:
        return True
    short = sum(1 for ln in lines if len(ln.split()) <= 3)
    nav = sum(1 for w in words if w in _NAV_WORDS)
    return (len(lines) >= 8 and short / len(lines) > 0.8) or nav / len(words) > 0.3


def run_checks(task: AnyTask, outcome: EngineOutcome, now: datetime) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    ev = outcome.evidence
    min_results = task.requirements.min_results
    if task.operation == "crawl":
        kept = int(outcome.coverage.get("pages_kept", len(ev)) or 0)
        checks.append(_check("pages_stored", kept >= 1, True, f"{kept} page(s) stored or confirmed unchanged"))
        return checks
    if task.operation == "search":
        checks.append(
            _check(
                "evidence_present",
                len(ev) >= max(1, min_results),
                True,
                f"{len(ev)} evidence item(s); at least {max(1, min_results)} required",
            )
        )
    else:
        text = outcome.content or ""
        stored = outcome.coverage.get("kind") == "stored"  # your own document: short is fine, it is not boilerplate
        checks.append(
            _check("content_non_empty", len(text.strip()) >= (1 if stored else 50), True, f"{len(text.strip())} characters")
        )
        if text.strip() and not stored:
            nav = navigation_only(text)
            checks.append(
                _check(
                    "not_navigation_only",
                    not nav,
                    True,
                    "extracted text looks like navigation/boilerplate" if nav else "body text present",
                )
            )
    if ev:
        missing = [e.source_id for e in ev if not e.version_digest or not (e.original_url or e.local_identifier)]
        checks.append(
            _check(
                "provenance_complete",
                not missing,
                True,
                "every item has a version digest and a source identifier"
                if not missing
                else f"{len(missing)} item(s) lack digest or identifier",
            )
        )
        keys = [e.canonical_url or e.local_identifier or e.source_id for e in ev]
        dupes = len(keys) - len(set(keys))
        checks.append(_check("duplicate_sources", dupes == 0, False, f"{dupes} duplicate source(s)"))
    max_age = task.requirements.max_age_seconds
    if max_age is not None and ev:
        stale = []
        for e in ev:
            stamp = parse_dt(e.fetched_at) or parse_dt(e.indexed_at) or parse_dt(e.imported_at)
            if stamp is None or (now - stamp).total_seconds() > max_age:
                stale.append(e.source_id)
        checks.append(
            _check(
                "freshness",
                not stale,
                True,
                f"all items retrieved within {max_age}s"
                if not stale
                else f"{len(stale)} item(s) older than {max_age}s by retrieval time",
            )
        )
    if task.operation == "search" and ev:
        query = getattr(task.input, "query", "")
        terms = set(_WORD.findall(query.lower()))
        if terms:
            top = (ev[0].title or "") + " " + ev[0].excerpt
            found = set(_WORD.findall(top.lower())) & terms
            ratio = len(found) / len(terms)
            checks.append(
                _check(
                    "lexical_relevance",
                    ratio >= 0.34,
                    False,
                    f"{len(found)}/{len(terms)} query terms in the top result (proxy only)",
                )
            )
    return checks


def required_passed(checks: list[dict[str, Any]]) -> bool:
    return all(c["passed"] for c in checks if c["required"])
