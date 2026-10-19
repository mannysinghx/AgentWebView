"""SQLite storage: one file, write-ahead logging, serialized writes.

Every write that checks a limit runs inside ``BEGIN IMMEDIATE``, so two calls can never both spend the
last unit of an allowance. Transactions nest: an inner ``tx()`` on the same thread joins the outer one.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agents (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  token_hash TEXT NOT NULL UNIQUE,
  token_hint TEXT NOT NULL,
  abilities TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL,
  last_used_at TEXT,
  revoked_at TEXT
);

CREATE TABLE IF NOT EXISTS provider_keys (
  id TEXT PRIMARY KEY,
  provider_id TEXT NOT NULL,
  label TEXT NOT NULL,
  secret BLOB NOT NULL,
  key_hint TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'unverified' CHECK (status IN ('unverified', 'ok', 'invalid')),
  status_detail TEXT,
  checked_at TEXT,
  balance TEXT,
  enabled INTEGER NOT NULL DEFAULT 1,
  last_used_at TEXT,
  created_at TEXT NOT NULL
);

-- Limits: global allowances, per-agent request limits and the credit on each provider key.
-- scope_id is '' for global limits so the UNIQUE constraint applies to them too.
CREATE TABLE IF NOT EXISTS limits (
  id TEXT PRIMARY KEY,
  scope TEXT NOT NULL CHECK (scope IN ('global', 'agent', 'provider_key')),
  scope_id TEXT NOT NULL DEFAULT '',
  denomination TEXT NOT NULL,
  period TEXT NOT NULL CHECK (period IN ('monthly', 'daily', 'lifetime')),
  amount INTEGER NOT NULL CHECK (amount >= 0),
  expires_at TEXT,
  name TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE (scope, scope_id, denomination, period)
);

CREATE TABLE IF NOT EXISTS limit_usage (
  limit_id TEXT NOT NULL REFERENCES limits(id) ON DELETE CASCADE,
  period_key TEXT NOT NULL,
  period_end TEXT,
  used INTEGER NOT NULL DEFAULT 0,
  held INTEGER NOT NULL DEFAULT 0,
  written_off INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (limit_id, period_key)
);

CREATE TABLE IF NOT EXISTS reservations (
  id TEXT PRIMARY KEY,
  task_id TEXT,
  engine_id TEXT,
  state TEXT NOT NULL CHECK (state IN ('held', 'settled', 'released')),
  lines TEXT NOT NULL,
  amounts TEXT NOT NULL,
  actual TEXT,
  created_at TEXT NOT NULL,
  settled_at TEXT
);

CREATE TABLE IF NOT EXISTS tasks (
  id TEXT PRIMARY KEY,
  agent_id TEXT,
  origin TEXT NOT NULL,
  operation TEXT NOT NULL,
  summary TEXT,
  request TEXT NOT NULL,
  status TEXT NOT NULL,
  engine_id TEXT,
  provider_key_id TEXT,
  error_code TEXT,
  usage TEXT,
  result TEXT,
  trace TEXT NOT NULL DEFAULT '[]',
  cancel_requested INTEGER NOT NULL DEFAULT 0,
  owner_pid INTEGER,
  created_at TEXT NOT NULL,
  completed_at TEXT,
  duration_ms INTEGER
);
CREATE INDEX IF NOT EXISTS tasks_by_time ON tasks (created_at);
CREATE INDEX IF NOT EXISTS tasks_by_agent ON tasks (agent_id, created_at);

CREATE TABLE IF NOT EXISTS attempts (
  task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  attempt_no INTEGER NOT NULL,
  engine_id TEXT NOT NULL,
  provider_key_id TEXT,
  state TEXT NOT NULL,
  error_code TEXT,
  usage TEXT,
  quality TEXT,
  latency_ms REAL,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  PRIMARY KEY (task_id, attempt_no)
);

CREATE TABLE IF NOT EXISTS engine_health (
  engine_id TEXT NOT NULL,
  scope_key TEXT NOT NULL DEFAULT '',
  circuit_state TEXT NOT NULL DEFAULT 'CLOSED',
  open_until TEXT,
  cooldown_until TEXT,
  consecutive_transport_errors INTEGER NOT NULL DEFAULT 0,
  successes INTEGER NOT NULL DEFAULT 0,
  failures INTEGER NOT NULL DEFAULT 0,
  latency_ewma_ms REAL,
  quality_passes INTEGER NOT NULL DEFAULT 0,
  quality_checks INTEGER NOT NULL DEFAULT 0,
  last_error_code TEXT,
  last_success_at TEXT,
  last_failure_at TEXT,
  updated_at TEXT,
  PRIMARY KEY (engine_id, scope_key)
);

CREATE TABLE IF NOT EXISTS evaluations (
  task_id TEXT PRIMARY KEY,
  engine_id TEXT,
  provider_key_id TEXT,
  relevance TEXT NOT NULL CHECK (relevance IN ('relevant', 'partial', 'irrelevant')),
  score INTEGER NOT NULL CHECK (score BETWEEN 0 AND 2),
  outcome TEXT NOT NULL CHECK (outcome IN ('succeeded', 'failed', 'unknown')),
  notes TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cache_entries (
  key TEXT PRIMARY KEY,
  response TEXT NOT NULL,
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  stale_until TEXT NOT NULL,
  hits INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS collections (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  description TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS documents (
  id TEXT PRIMARY KEY,
  collection_id TEXT NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
  title TEXT,
  source_type TEXT NOT NULL,
  local_identifier TEXT NOT NULL,
  original_url TEXT,
  canonical_url TEXT,
  media_type TEXT,
  extraction_method TEXT,
  content_digest TEXT NOT NULL,
  body TEXT NOT NULL,
  chars INTEGER NOT NULL,
  published_at TEXT,
  fetched_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE (collection_id, local_identifier)
);

CREATE TABLE IF NOT EXISTS chunks (
  id INTEGER PRIMARY KEY,
  document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  ordinal INTEGER NOT NULL,
  start_offset INTEGER NOT NULL,
  end_offset INTEGER NOT NULL,
  text TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS chunks_by_document ON chunks (document_id);

CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(title, body, tokenize = 'porter unicode61');

CREATE TRIGGER IF NOT EXISTS chunks_fts_delete AFTER DELETE ON chunks BEGIN
  DELETE FROM chunks_fts WHERE rowid = old.id;
END;

CREATE TABLE IF NOT EXISTS crawls (
  id TEXT PRIMARY KEY,
  collection_id TEXT NOT NULL,
  agent_id TEXT,
  input TEXT NOT NULL,
  state TEXT NOT NULL,
  stats TEXT NOT NULL DEFAULT '{}',
  stop_reason TEXT,
  created_at TEXT NOT NULL,
  finished_at TEXT
);

CREATE TABLE IF NOT EXISTS browser_sessions (
  id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL,
  agent_id TEXT,
  state TEXT NOT NULL,
  start_url TEXT,
  allowed_hosts TEXT NOT NULL,
  capabilities TEXT NOT NULL DEFAULT '[]',
  current_url TEXT,
  title TEXT,
  owner_pid INTEGER,
  browser_ms INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  last_action_at TEXT,
  closed_at TEXT,
  close_reason TEXT
);

CREATE TABLE IF NOT EXISTS browser_actions (
  id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL REFERENCES browser_sessions(id) ON DELETE CASCADE,
  seq INTEGER NOT NULL,
  type TEXT NOT NULL,
  params TEXT NOT NULL,
  state TEXT NOT NULL,
  write_category TEXT,
  element TEXT,
  page_url TEXT,
  result TEXT,
  error TEXT,
  screenshot TEXT,
  decision_note TEXT,
  created_at TEXT NOT NULL,
  decided_at TEXT,
  finished_at TEXT
);
CREATE INDEX IF NOT EXISTS browser_actions_by_session ON browser_actions (session_id, seq);
"""


def utcnow() -> datetime:
    return datetime.now(UTC)


def iso(dt: datetime | None) -> str | None:
    return dt.astimezone(UTC).isoformat() if dt is not None else None


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def new_id(prefix: str = "") -> str:
    return prefix + uuid.uuid4().hex


def dumps(value: Any) -> str:
    return json.dumps(value, default=str, separators=(",", ":"))


def loads(value: str | None, default: Any = None) -> Any:
    if value is None or value == "":
        return default
    return json.loads(value)


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._local = threading.local()
        conn = self._conn()
        conn.executescript(SCHEMA)
        row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
        if row is None:
            conn.execute("INSERT INTO meta (key, value) VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))

    def _conn(self) -> sqlite3.Connection:
        conn: sqlite3.Connection | None = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=30, isolation_level=None, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA busy_timeout = 30000")
            self._local.conn = conn
            self._local.depth = 0
        return conn

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """A write transaction (``BEGIN IMMEDIATE``); nested calls on one thread share it."""
        conn = self._conn()
        depth = getattr(self._local, "depth", 0)
        if depth:
            self._local.depth = depth + 1
            try:
                yield conn
            finally:
                self._local.depth -= 1
            return
        conn.execute("BEGIN IMMEDIATE")
        self._local.depth = 1
        try:
            yield conn
        except BaseException:
            self._local.depth = 0
            conn.execute("ROLLBACK")
            raise
        self._local.depth = 0
        conn.execute("COMMIT")

    def read(self) -> sqlite3.Connection:
        """The thread's connection for plain reads (autocommit, or inside the current transaction)."""
        return self._conn()

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    # ------------------------------------------------------------------ small helpers

    def get_meta(self, key: str) -> str | None:
        row = self.read().execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return None if row is None else str(row["value"])

    def set_meta(self, key: str, value: str) -> None:
        with self.tx() as conn:
            conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
