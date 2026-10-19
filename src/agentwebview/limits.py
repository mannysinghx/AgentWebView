"""Limits: checked and reserved before every call, settled with what the call actually used.

Three kinds of limit, all counted per calendar period (monthly, daily, or lifetime) in the policy's time zone:

* **global** limits on built-in units (``task_calls``, ``search_calls``, ``fetched_pages``, ``browser_ms``).
  A built-in unit with no global limit is unlimited.
* **agent** limits: requests (``task_calls``) per month and per day for one agent.
* **provider_key** allowances: the credit on one provider key, in that provider's own unit (USD micro-units,
  credits, searches or tokens). A provider key with no allowance is never used: every provider call must fit
  inside the credit you said the key has, so nothing is spent that you did not plan for.

A reservation holds every applicable limit at once inside one ``BEGIN IMMEDIATE`` transaction; if any limit
is short, nothing is held and the shortfalls are explained. Settling replaces the hold with the measured use.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from agentwebview.db import Database, dumps, iso, loads, new_id, parse_dt
from agentwebview.errors import ErrorCode, GatewayError

BUILTIN_UNITS = ("task_calls", "search_calls", "fetched_pages", "browser_ms")
UNIT_LABELS = {
    "task_calls": "requests",
    "search_calls": "searches",
    "fetched_pages": "pages read",
    "browser_ms": "browser time",
}
# Conservative monthly starting limits; change them on the Settings screen.
DEFAULT_GLOBAL_LIMITS: dict[str, int] = {
    "task_calls": 100_000,
    "search_calls": 100_000,
    "fetched_pages": 10_000,
    "browser_ms": 3_600_000,
}
LIFETIME_START = datetime(1970, 1, 1, tzinfo=UTC)


@dataclass
class Shortfall:
    scope: str
    limit_id: str | None
    name: str | None
    denomination: str
    period_key: str | None
    available: int
    requested: int
    reason: str
    resets_at: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "scope": self.scope,
            "limit_id": self.limit_id,
            "name": self.name,
            "denomination": self.denomination,
            "period": self.period_key,
            "available": str(self.available),
            "requested": str(self.requested),
            "reason": self.reason,
            "resets_at": self.resets_at,
        }

    def sentence(self) -> str:
        what = self.name or f"{self.scope} {self.denomination}"
        if self.reason == "no_allowance":
            return f"no allowance is recorded for this key in {self.denomination}"
        tail = f"; resets {self.resets_at}" if self.resets_at else ""
        return f"{what}: {self.available} left, needs {self.requested}{tail}"


def _zone(tz_name: str) -> ZoneInfo:
    try:
        return ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def _local_midnight(d: date, tz: ZoneInfo) -> datetime:
    return datetime(d.year, d.month, d.day, tzinfo=tz).astimezone(UTC)


def period_for(period: str, tz_name: str, at: datetime) -> tuple[str, datetime, datetime | None]:
    """(period_key, starts_at_utc, ends_at_utc) for the period containing ``at``."""
    if period == "lifetime":
        return "lifetime", LIFETIME_START, None
    tz = _zone(tz_name)
    local = at.astimezone(tz)
    if period == "monthly":
        first = date(local.year, local.month, 1)
        nxt = date(local.year + 1, 1, 1) if local.month == 12 else date(local.year, local.month + 1, 1)
        return f"{first.year:04d}-{first.month:02d}", _local_midnight(first, tz), _local_midnight(nxt, tz)
    if period == "daily":
        day = local.date()
        return day.isoformat(), _local_midnight(day, tz), _local_midnight(day + timedelta(days=1), tz)
    raise ValueError(f"unknown period {period}")


def expired(limit: Any, now: datetime) -> bool:
    exp = parse_dt(limit["expires_at"])
    return exp is not None and exp <= now


def ensure_defaults(db: Database) -> None:
    """Create the default global limits on first start (never again, so deleting one makes it unlimited)."""
    if db.get_meta("default_limits_created") is not None:
        return
    now = iso(datetime.now(UTC))
    with db.tx() as conn:
        for unit, amount in DEFAULT_GLOBAL_LIMITS.items():
            conn.execute(
                "INSERT OR IGNORE INTO limits (id, scope, scope_id, denomination, period, amount, name, created_at) "
                "VALUES (?, 'global', '', ?, 'monthly', ?, ?, ?)",
                (new_id("lim_"), unit, amount, f"Monthly {UNIT_LABELS[unit]}", now),
            )
        conn.execute(
            "INSERT INTO meta (key, value) VALUES ('default_limits_created', ?) "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            (now,),
        )


# --------------------------------------------------------------------------- reading


def applicable(
    conn: Any, denominations: list[str], agent_id: str | None, key_id: str | None, now: datetime
) -> list[Any]:
    if not denominations:
        return []
    marks = ",".join("?" for _ in denominations)
    rows = conn.execute(
        f"SELECT * FROM limits WHERE denomination IN ({marks}) AND ("  # noqa: S608 - placeholders only
        " scope = 'global'"
        " OR (scope = 'agent' AND scope_id = ?)"
        " OR (scope = 'provider_key' AND scope_id = ?)"
        ") ORDER BY id",
        (*denominations, agent_id or "\x00", key_id or "\x00"),
    ).fetchall()
    return [r for r in rows if not expired(r, now)]


def usage_row(conn: Any, limit: Any, now: datetime, tz: str) -> dict[str, Any]:
    key, _start, end = period_for(limit["period"], tz, now)
    row = conn.execute(
        "SELECT used, held, written_off FROM limit_usage WHERE limit_id = ? AND period_key = ?", (limit["id"], key)
    ).fetchone()
    used, held, off = (int(row["used"]), int(row["held"]), int(row["written_off"])) if row else (0, 0, 0)
    ends = [d for d in (end, parse_dt(limit["expires_at"])) if d is not None]
    return {
        "period_key": key,
        "period_end": iso(end),
        "resets_at": iso(min(ends)) if ends else None,
        "used": used,
        "held": held,
        "written_off": off,
        "available": int(limit["amount"]) - used - held - off,
    }


def view(conn: Any, limit: Any, now: datetime, tz: str) -> dict[str, Any]:
    u = usage_row(conn, limit, now, tz)
    return {
        "id": limit["id"],
        "scope": limit["scope"],
        "scope_id": limit["scope_id"] or None,
        "denomination": limit["denomination"],
        "period": limit["period"],
        "amount": int(limit["amount"]),
        "name": limit["name"],
        "expires_at": limit["expires_at"],
        "expired": expired(limit, now),
        "period_key": u["period_key"],
        "resets_at": u["resets_at"],
        "used": u["used"],
        "held": u["held"],
        "written_off": u["written_off"],
        "remaining": max(0, u["available"]),
    }


def probe(
    conn: Any,
    amounts: dict[str, int],
    *,
    agent_id: str | None,
    key_id: str | None,
    provider_units: frozenset[str],
    now: datetime,
    tz: str,
) -> list[Shortfall]:
    """Read-only check of every limit a call would touch."""
    positive = {d: a for d, a in amounts.items() if a > 0}
    rows = applicable(conn, list(positive), agent_id, key_id, now)
    by_denom: dict[str, list[Any]] = defaultdict(list)
    for r in rows:
        by_denom[r["denomination"]].append(r)
    out: list[Shortfall] = []
    for d, amt in positive.items():
        mine = by_denom.get(d, [])
        if d in provider_units and not any(r["scope"] == "provider_key" for r in mine):
            out.append(Shortfall("provider_key", None, None, d, None, 0, amt, "no_allowance"))
            continue
        for r in mine:
            u = usage_row(conn, r, now, tz)
            if u["available"] < amt:
                out.append(
                    Shortfall(
                        r["scope"],
                        r["id"],
                        r["name"],
                        d,
                        u["period_key"],
                        max(0, u["available"]),
                        amt,
                        "exhausted",
                        u["resets_at"],
                    )
                )
    return out


def budget_exhausted(shortfalls: list[Shortfall]) -> GatewayError:
    resets = sorted(s.resets_at for s in shortfalls if s.resets_at)
    return GatewayError(
        ErrorCode.BUDGET_EXHAUSTED,
        "a limit would be exceeded: " + "; ".join(s.sentence() for s in shortfalls[:4]),
        details={"shortfalls": [s.as_dict() for s in shortfalls]},
        remediation="wait for the next period, raise the limit on the Settings or Agents screen, "
        "or add credit to a provider key",
        retryable=bool(resets),
    )


# --------------------------------------------------------------------------- writing


def reserve(
    db: Database,
    *,
    task_id: str | None,
    engine_id: str,
    amounts: dict[str, int],
    agent_id: str | None,
    key_id: str | None,
    provider_units: frozenset[str],
    now: datetime,
    tz: str,
) -> str:
    """Hold every applicable limit at once, or nothing (raises BUDGET_EXHAUSTED with the shortfalls)."""
    positive = {d: int(a) for d, a in amounts.items() if int(a) > 0}
    with db.tx() as conn:
        shortfalls = probe(
            conn, positive, agent_id=agent_id, key_id=key_id, provider_units=provider_units, now=now, tz=tz
        )
        if shortfalls:
            raise budget_exhausted(shortfalls)
        lines: list[dict[str, Any]] = []
        for r in applicable(conn, list(positive), agent_id, key_id, now):
            amt = positive[r["denomination"]]
            key, _s, end = period_for(r["period"], tz, now)
            conn.execute(
                "INSERT INTO limit_usage (limit_id, period_key, period_end, used, held) VALUES (?, ?, ?, 0, ?) "
                "ON CONFLICT (limit_id, period_key) DO UPDATE SET held = held + excluded.held",
                (r["id"], key, iso(end), amt),
            )
            lines.append({"limit_id": r["id"], "period_key": key, "denomination": r["denomination"], "amount": amt})
        rid = new_id("res_")
        conn.execute(
            "INSERT INTO reservations (id, task_id, engine_id, state, lines, amounts, created_at) "
            "VALUES (?, ?, ?, 'held', ?, ?, ?)",
            (rid, task_id, engine_id, dumps(lines), dumps(positive), iso(now)),
        )
    return rid


def settle(db: Database, reservation_id: str, actual: dict[str, int], now: datetime) -> dict[str, int]:
    """Replace the hold with measured use. Idempotent. Returns any overrun (used more than reserved)."""
    with db.tx() as conn:
        row = conn.execute("SELECT * FROM reservations WHERE id = ?", (reservation_id,)).fetchone()
        if row is None or row["state"] != "held":
            return {}
        reserved = {k: int(v) for k, v in loads(row["amounts"], {}).items()}
        for line in loads(row["lines"], []):
            used = int(actual.get(line["denomination"], 0))
            conn.execute(
                "UPDATE limit_usage SET held = MAX(0, held - ?), used = used + ? WHERE limit_id = ? AND period_key = ?",
                (int(line["amount"]), max(0, used), line["limit_id"], line["period_key"]),
            )
        conn.execute(
            "UPDATE reservations SET state = 'settled', actual = ?, settled_at = ? WHERE id = ?",
            (dumps({k: int(v) for k, v in actual.items()}), iso(now), reservation_id),
        )
    return {d: int(v) - reserved.get(d, 0) for d, v in actual.items() if int(v) > reserved.get(d, 0) and d in reserved}


def release(db: Database, reservation_id: str, now: datetime) -> None:
    with db.tx() as conn:
        row = conn.execute("SELECT * FROM reservations WHERE id = ?", (reservation_id,)).fetchone()
        if row is None or row["state"] != "held":
            return
        for line in loads(row["lines"], []):
            conn.execute(
                "UPDATE limit_usage SET held = MAX(0, held - ?) WHERE limit_id = ? AND period_key = ?",
                (int(line["amount"]), line["limit_id"], line["period_key"]),
            )
        conn.execute("UPDATE reservations SET state = 'released', settled_at = ? WHERE id = ?", (iso(now), reservation_id))


STALE_HOLD = timedelta(hours=2)


def release_stale(db: Database, now: datetime) -> int:
    """At start-up: holds older than any call can run (a process stopped mid-call) are released. Younger
    holds may belong to another running AgentWebView process (a stdio MCP server) and are left alone."""
    cutoff = iso(now - STALE_HOLD)
    rows = db.read().execute("SELECT id FROM reservations WHERE state = 'held' AND created_at < ?", (cutoff,)).fetchall()
    for r in rows:
        release(db, r["id"], now)
    return len(rows)


def write_off(db: Database, key_id: str, unit: str, now: datetime, tz: str) -> int:
    """The provider said this key's credit is gone: the rest of this period's allowance is written off."""
    total = 0
    with db.tx() as conn:
        rows = conn.execute(
            "SELECT * FROM limits WHERE scope = 'provider_key' AND scope_id = ? AND denomination = ?", (key_id, unit)
        ).fetchall()
        for r in rows:
            u = usage_row(conn, r, now, tz)
            left = u["available"]
            if left <= 0:
                continue
            key, _s, end = period_for(r["period"], tz, now)
            conn.execute(
                "INSERT INTO limit_usage (limit_id, period_key, period_end, written_off) VALUES (?, ?, ?, ?) "
                "ON CONFLICT (limit_id, period_key) DO UPDATE SET written_off = written_off + excluded.written_off",
                (r["id"], key, iso(end), left),
            )
            total += left
    return total


def free_state(conn: Any, key_id: str, unit: str, now: datetime, tz: str) -> tuple[int | None, datetime | None]:
    """(credit left, soonest instant the credit being spent expires) for a provider key, or (None, None)."""
    rows = [
        r
        for r in conn.execute(
            "SELECT * FROM limits WHERE scope = 'provider_key' AND scope_id = ? AND denomination = ?", (key_id, unit)
        ).fetchall()
        if not expired(r, now)
    ]
    if not rows:
        return None, None
    remaining = 0
    soonest: datetime | None = None
    for r in rows:
        u = usage_row(conn, r, now, tz)
        left = max(0, u["available"])
        remaining += left
        if left <= 0 or not u["resets_at"]:
            continue
        ends = parse_dt(u["resets_at"])
        if ends is not None and (soonest is None or ends < soonest):
            soonest = ends
    return remaining, soonest


def set_limit(
    db: Database,
    *,
    scope: str,
    scope_id: str,
    denomination: str,
    period: str,
    amount: int | None,
    name: str,
    expires_at: str | None = None,
) -> None:
    """Create, change (amount None = remove) one limit. Usage already counted in the period is kept."""
    with db.tx() as conn:
        row = conn.execute(
            "SELECT id FROM limits WHERE scope = ? AND scope_id = ? AND denomination = ? AND period = ?",
            (scope, scope_id, denomination, period),
        ).fetchone()
        if amount is None:
            if row is not None:
                conn.execute("DELETE FROM limits WHERE id = ?", (row["id"],))
            return
        if row is None:
            conn.execute(
                "INSERT INTO limits (id, scope, scope_id, denomination, period, amount, expires_at, name, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (new_id("lim_"), scope, scope_id, denomination, period, int(amount), expires_at, name, iso(datetime.now(UTC))),
            )
        else:
            conn.execute(
                "UPDATE limits SET amount = ?, expires_at = ?, name = ? WHERE id = ?",
                (int(amount), expires_at, name, row["id"]),
            )
