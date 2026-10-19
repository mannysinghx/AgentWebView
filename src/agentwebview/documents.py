"""Your own documents: collections, imported or crawled pages, and lexical full-text search (SQLite FTS5).

Retrieval is lexical and reported as such; there is no semantic retrieval. A passage must contain at least
a third of the query's words, so a long question does not match on one common word; a passage containing
every word outranks one containing only some of them.
"""

from __future__ import annotations

import math
import re
from datetime import datetime
from typing import Any

from agentwebview.db import Database, iso, new_id
from agentwebview.errors import ErrorCode, GatewayError, invalid, not_found
from agentwebview.extract import Extracted, chunk_text

MAX_CHUNK_CHARS = 1_500
MAX_DOCUMENT_CHARS = 2_000_000
MAX_UPLOAD_BYTES = 10_000_000
_TOKEN = re.compile(r"\w+", re.UNICODE)
_NEGATED = re.compile(r'(?:(?<=\s)|^)-("[^"]*"|\S+)')
# English stop words (the PostgreSQL 'english' stop-word list), dropped from queries so "how long do
# refunds take" is matched on its meaningful words.
STOP_WORDS = frozenset(
    "i me my myself we our ours ourselves you your yours yourself yourselves he him his himself she her hers herself "
    "it its itself they them their theirs themselves what which who whom this that these those am is are was were be "
    "been being have has had having do does did doing a an the and but if or because as until while of at by for with "
    "about against between into through during before after above below to from up down in out on off over under "
    "again further then once here there when where why how all any both each few more most other some such no nor not "
    "only own same so than too very s t can will just don should now".split()
)


# --------------------------------------------------------------------------- collections


def collection_view(row: Any, docs: int = 0, chars: int = 0) -> dict[str, Any]:
    return {
        "id": row["id"],
        "name": row["name"],
        "description": row["description"],
        "created_at": row["created_at"],
        "documents": docs,
        "chars": chars,
    }


def list_collections(conn: Any) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT c.*, COUNT(d.id) AS docs, COALESCE(SUM(d.chars), 0) AS chars FROM collections c "
        "LEFT JOIN documents d ON d.collection_id = c.id GROUP BY c.id ORDER BY c.name"
    ).fetchall()
    return [collection_view(r, int(r["docs"]), int(r["chars"])) for r in rows]


def create_collection(db: Database, name: str, description: str | None, now: datetime) -> dict[str, Any]:
    name = " ".join((name or "").split())[:120]
    if not name:
        raise invalid("a collection needs a name")
    with db.tx() as conn:
        if conn.execute("SELECT 1 FROM collections WHERE name = ?", (name,)).fetchone():
            raise GatewayError(ErrorCode.CONFLICT, f"a collection named '{name}' already exists")
        cid = new_id("col_")
        conn.execute(
            "INSERT INTO collections (id, name, description, created_at) VALUES (?, ?, ?, ?)",
            (cid, name, (description or "")[:500] or None, iso(now)),
        )
        row = conn.execute("SELECT * FROM collections WHERE id = ?", (cid,)).fetchone()
    return collection_view(row)


def resolve_collection(conn: Any, ref: str) -> Any:
    row = conn.execute("SELECT * FROM collections WHERE id = ? OR name = ?", (ref, ref)).fetchone()
    if row is None:
        raise not_found(f"collection '{ref}'")
    return row


def delete_collection(db: Database, collection_id: str) -> None:
    with db.tx() as conn:
        row = conn.execute("SELECT id FROM collections WHERE id = ?", (collection_id,)).fetchone()
        if row is None:
            raise not_found("collection")
        conn.execute("DELETE FROM chunks WHERE document_id IN (SELECT id FROM documents WHERE collection_id = ?)", (collection_id,))
        conn.execute("DELETE FROM documents WHERE collection_id = ?", (collection_id,))
        conn.execute("DELETE FROM collections WHERE id = ?", (collection_id,))


# --------------------------------------------------------------------------- documents


def document_view(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "collection_id": row["collection_id"],
        "title": row["title"],
        "source_type": row["source_type"],
        "local_identifier": row["local_identifier"],
        "original_url": row["original_url"],
        "media_type": row["media_type"],
        "chars": row["chars"],
        "fetched_at": row["fetched_at"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def list_documents(conn: Any, collection_id: str, limit: int = 500) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM documents WHERE collection_id = ? ORDER BY updated_at DESC LIMIT ?", (collection_id, limit)
    ).fetchall()
    return [document_view(r) for r in rows]


def delete_document(db: Database, document_id: str) -> None:
    with db.tx() as conn:
        if conn.execute("SELECT 1 FROM documents WHERE id = ?", (document_id,)).fetchone() is None:
            raise not_found("document")
        conn.execute("DELETE FROM chunks WHERE document_id = ?", (document_id,))
        conn.execute("DELETE FROM documents WHERE id = ?", (document_id,))


def ingest(
    conn: Any,
    collection_id: str,
    *,
    local_identifier: str,
    source_type: str,
    extracted: Extracted,
    original_url: str | None,
    now: datetime,
    fetched_at: datetime | None = None,
) -> dict[str, Any]:
    """Store or replace one document (call inside a transaction). Unchanged content is not re-indexed."""
    text = extracted.text[:MAX_DOCUMENT_CHARS]
    existing = conn.execute(
        "SELECT * FROM documents WHERE collection_id = ? AND local_identifier = ?", (collection_id, local_identifier)
    ).fetchone()
    if existing is not None and existing["content_digest"] == extracted.content_digest:
        conn.execute(
            "UPDATE documents SET fetched_at = COALESCE(?, fetched_at), updated_at = ? WHERE id = ?",
            (iso(fetched_at), iso(now), existing["id"]),
        )
        return {"status": "unchanged", "document_id": existing["id"], "version_digest": existing["content_digest"]}
    if existing is not None:
        doc_id = existing["id"]
        conn.execute("DELETE FROM chunks WHERE document_id = ?", (doc_id,))
        conn.execute(
            "UPDATE documents SET title = ?, source_type = ?, original_url = ?, canonical_url = ?, media_type = ?, "
            "extraction_method = ?, content_digest = ?, body = ?, chars = ?, published_at = ?, fetched_at = ?, "
            "updated_at = ? WHERE id = ?",
            (
                extracted.title,
                source_type,
                original_url,
                extracted.canonical_url,
                extracted.media_type,
                extracted.method,
                extracted.content_digest,
                text,
                len(text),
                iso(extracted.published_at),
                iso(fetched_at),
                iso(now),
                doc_id,
            ),
        )
        status = "updated"
    else:
        doc_id = new_id("doc_")
        conn.execute(
            "INSERT INTO documents (id, collection_id, title, source_type, local_identifier, original_url, "
            "canonical_url, media_type, extraction_method, content_digest, body, chars, published_at, fetched_at, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                doc_id,
                collection_id,
                extracted.title,
                source_type,
                local_identifier,
                original_url,
                extracted.canonical_url,
                extracted.media_type,
                extracted.method,
                extracted.content_digest,
                text,
                len(text),
                iso(extracted.published_at),
                iso(fetched_at),
                iso(now),
                iso(now),
            ),
        )
        status = "stored"
    for ordinal, (start, end, body) in enumerate(chunk_text(text, max_chars=MAX_CHUNK_CHARS)):
        cur = conn.execute(
            "INSERT INTO chunks (document_id, ordinal, start_offset, end_offset, text) VALUES (?, ?, ?, ?, ?)",
            (doc_id, ordinal, start, end, body),
        )
        conn.execute(
            "INSERT INTO chunks_fts (rowid, title, body) VALUES (?, ?, ?)", (cur.lastrowid, extracted.title or "", body)
        )
    return {"status": status, "document_id": doc_id, "version_digest": extracted.content_digest}


# --------------------------------------------------------------------------- search


def query_terms(query: str) -> tuple[list[str], list[str]]:
    negated_text = " ".join(m.group(1).strip('"') for m in _NEGATED.finditer(query))
    positive_text = _NEGATED.sub(" ", query).replace('"', " ")
    words = list(dict.fromkeys(t.lower() for t in _TOKEN.findall(positive_text)))
    positive = [w for w in words if w not in STOP_WORDS] or words  # a query of only stop words keeps them
    negative = [t for t in dict.fromkeys(t.lower() for t in _TOKEN.findall(negated_text)) if t not in positive]
    return positive, negative


def _fts_query(positive: list[str], negative: list[str]) -> str:
    def quote(t: str) -> str:
        return '"' + t.replace('"', '""') + '"'

    q = " OR ".join(quote(t) for t in positive)
    if negative:
        q = f"({q}) NOT ({' OR '.join(quote(t) for t in negative)})"
    return q


def _matched(terms: list[str], text: str) -> int:
    low = text.lower()
    return sum(1 for t in terms if re.search(r"\b" + re.escape(t[: max(3, len(t) - 2)]), low))


def _excerpt(text: str, terms: list[str], max_chars: int) -> str:
    low = text.lower()
    first = min((m.start() for t in terms for m in [re.search(r"\b" + re.escape(t), low)] if m), default=0)
    start = max(0, first - max_chars // 4)
    if start:
        space = text.find(" ", start)
        start = space + 1 if 0 <= space < first else start
    snippet = " ".join(text[start : start + max_chars].split())
    return ("… " if start else "") + snippet


def search(conn: Any, query: str, collection_ids: list[str], limit: int, max_excerpt: int) -> list[dict[str, Any]]:
    positive, negative = query_terms(query)
    if not positive:
        return []
    min_terms = max(1, math.ceil(len(positive) / 3))
    params: list[Any] = [_fts_query(positive, negative)]
    where = ""
    if collection_ids:
        where = f" AND d.collection_id IN ({','.join('?' for _ in collection_ids)})"
        params.extend(collection_ids)
    rows = conn.execute(
        "SELECT c.id AS chunk_id, c.text, c.start_offset, c.end_offset, bm25(chunks_fts, 2.0, 1.0) AS bm, "
        "d.id AS document_id, d.collection_id, d.title, d.source_type, d.local_identifier, d.original_url, "
        "d.canonical_url, d.content_digest, d.extraction_method, d.published_at, d.fetched_at, d.created_at, "
        "d.updated_at, d.chars "
        "FROM chunks_fts JOIN chunks c ON c.id = chunks_fts.rowid JOIN documents d ON d.id = c.document_id "
        f"WHERE chunks_fts MATCH ?{where} ORDER BY bm LIMIT 500",  # noqa: S608 - placeholders only
        params,
    ).fetchall()
    best: dict[str, tuple[tuple[float, float, float], Any, int]] = {}
    for r in rows:
        matched = _matched(positive, (r["title"] or "") + " " + r["text"])
        if matched < min_terms:
            continue
        strict = 1.0 if matched >= len(positive) else 0.0
        rank = (strict, matched / len(positive), -float(r["bm"]))
        prev = best.get(r["document_id"])
        if prev is None or rank > prev[0]:
            best[r["document_id"]] = (rank, r, matched)
    ordered = sorted(best.values(), key=lambda x: (x[0], x[1]["document_id"]), reverse=True)[:limit]
    out = []
    for rank, r, matched in ordered:
        out.append(
            {
                "row": r,
                "score": round(rank[0] + rank[1] + min(1.0, rank[2] / 20), 6),
                "matched_terms": matched,
                "excerpt": _excerpt(r["text"], positive, max_excerpt),
            }
        )
    return out


def read_document(conn: Any, document_id: str) -> Any:
    row = conn.execute("SELECT * FROM documents WHERE id = ?", (document_id,)).fetchone()
    if row is None:
        raise not_found(f"document '{document_id}'")
    return row
