"""Import format detection and text extraction (UTF-8 text, Markdown, HTML).

Uploaded HTML is treated strictly as input text: it is parsed for its readable content and never
stored or served as active content.
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from agentwebview.urls import UrlError, canonicalize

log = logging.getLogger("agentwebview.extract")

ALLOWED_EXTENSIONS: dict[str, str] = {
    ".txt": "text/plain",
    ".text": "text/plain",
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".html": "text/html",
    ".htm": "text/html",
}
ALLOWED_MEDIA = frozenset({"text/plain", "text/markdown", "text/html", "application/xhtml+xml"})
_MAGIC_REJECT: list[tuple[bytes, str]] = [
    (b"MZ", "Windows executable"),
    (b"\x7fELF", "ELF executable"),
    (b"\xcf\xfa\xed\xfe", "Mach-O executable"),
    (b"\xfe\xed\xfa", "Mach-O executable"),
    (b"PK\x03\x04", "ZIP archive"),
    (b"\x1f\x8b", "gzip archive"),
    (b"%PDF", "PDF (not supported yet)"),
    (b"Rar!", "RAR archive"),
    (b"7z\xbc\xaf", "7z archive"),
    (b"\x00asm", "WebAssembly binary"),
    (b"#!", "script with interpreter line"),
]
MAX_HTML_TAGS = 100_000
MAX_NESTING_HINT = 2_000


class RejectedImport(ValueError):
    pass


@dataclass
class Extracted:
    text: str
    title: str | None
    media_type: str
    method: str
    raw_digest: str
    content_digest: str
    raw_bytes: int
    canonical_url: str | None = None
    published_at: datetime | None = None
    truncated: bool = False
    meta: dict[str, Any] = field(default_factory=dict)


def detect_media_type(name: str, declared: str | None) -> str:
    lower = name.lower()
    ext = next((e for e in ALLOWED_EXTENSIONS if lower.endswith(e)), None)
    declared = (declared or "").split(";")[0].strip().lower() or None
    if ext:
        return ALLOWED_EXTENSIONS[ext]
    if declared in ALLOWED_MEDIA:
        return "text/html" if declared == "application/xhtml+xml" else declared
    raise RejectedImport("unsupported file type; allowed: .txt, .md, .markdown, .html, .htm")


def decode_utf8(raw: bytes) -> str:
    for magic, label in _MAGIC_REJECT:
        if raw.startswith(magic):
            raise RejectedImport(f"rejected content: {label}")
    if b"\x00" in raw[:8192]:
        raise RejectedImport("rejected content: binary data (NUL bytes)")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise RejectedImport("content is not valid UTF-8 (only UTF-8 is accepted)") from exc
    return text.replace("\r\n", "\n").replace("\r", "\n")


def normalize_text(text: str) -> str:
    text = re.sub(r"[ \t\f\v]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def digest(value: str | bytes) -> str:
    data = value.encode("utf-8") if isinstance(value, str) else value
    return hashlib.sha256(data).hexdigest()


_MD_PATTERNS: list[tuple[re.Pattern[str], str | Callable[[re.Match[str]], str]]] = [
    (re.compile(r"```.*?```", re.S), lambda m: m.group(0).strip("`")),
    (re.compile(r"!\[([^\]]*)\]\([^)]*\)"), r"\1"),
    (re.compile(r"\[([^\]]+)\]\([^)]*\)"), r"\1"),
    (re.compile(r"^\s{0,3}#{1,6}\s*", re.M), ""),
    (re.compile(r"^\s{0,3}>\s?", re.M), ""),
    (re.compile(r"^\s*[-*+]\s+", re.M), "- "),
    (re.compile(r"(\*\*|__)(.+?)\1"), r"\2"),
    (re.compile(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])"), r"\1"),
    (re.compile(r"`([^`]+)`"), r"\1"),
    (re.compile(r"^\s*([-*_]\s*){3,}$", re.M), ""),
]


def _markdown_text(src: str) -> tuple[str, str | None]:
    title = None
    m = re.search(r"^\s{0,3}#\s+(.+?)\s*#*\s*$", src, re.M)
    if m:
        title = m.group(1).strip()
    out = src
    for pattern, repl in _MD_PATTERNS:
        out = pattern.sub(repl, out)
    return out, title


def _first_line_title(text: str) -> str | None:
    for line in text.splitlines():
        line = line.strip()
        if line:
            return line[:200]
    return None


_TRAF_CONFIG: Any = None


def _trafilatura_config() -> Any:
    """Bounded, offline extraction settings. Signal-based timeouts are off because extraction runs in
    threads; the task deadline and input size limits bound the work instead."""
    global _TRAF_CONFIG
    if _TRAF_CONFIG is None:
        from trafilatura.settings import use_config

        cfg = use_config()
        cfg.set("DEFAULT", "MAX_TREE_SIZE", str(MAX_NESTING_HINT * 50))
        cfg.set("DEFAULT", "EXTRACTION_TIMEOUT", "0")
        cfg.set("DEFAULT", "EXTERNAL_URLS", "off")
        _TRAF_CONFIG = cfg
    return _TRAF_CONFIG


def _html_text(src: str, original_url: str | None) -> tuple[str, str | None, str, str | None, datetime | None]:
    if src.count("<") > MAX_HTML_TAGS:
        raise RejectedImport("HTML is too complex to process safely (too many tags)")
    import trafilatura

    doc = trafilatura.bare_extraction(
        src,
        url=original_url,
        with_metadata=True,
        include_comments=False,
        include_tables=True,
        favor_recall=True,
        config=_trafilatura_config(),
    )
    text = (getattr(doc, "text", None) or "") if doc is not None else ""
    title = getattr(doc, "title", None) if doc is not None else None
    canonical = getattr(doc, "url", None) if doc is not None else None
    date_raw = getattr(doc, "date", None) if doc is not None else None
    method = "html-trafilatura"
    if len(text.strip()) < 40:
        # Weak extraction: fall back to all visible text (scripts/styles removed) and say so.
        from lxml import html as lxml_html

        try:
            tree = lxml_html.fromstring(src)
            for bad in tree.xpath("//script|//style|//noscript|//template"):
                bad.drop_tree()
            fallback = tree.text_content()
            if len(fallback.strip()) > len(text.strip()):
                text, method = fallback, "html-fallback-visible-text"
            if not title:
                t = tree.findtext(".//title")
                title = t.strip() if t else None
        except Exception as exc:  # unparseable HTML keeps whatever trafilatura produced
            log.debug("visible-text fallback failed: %s", type(exc).__name__)
    published = None
    if date_raw:
        try:
            published = datetime.fromisoformat(str(date_raw)).replace(tzinfo=UTC)
        except ValueError:
            published = None
    return text, title, method, canonical, published


def extract(
    name: str, raw: bytes, *, declared_media_type: str | None, original_url: str | None, max_chars: int
) -> Extracted:
    media = detect_media_type(name, declared_media_type)
    src = decode_utf8(raw)
    canonical: str | None = None
    published: datetime | None = None
    if media == "text/html":
        body, title, method, canonical_meta, published = _html_text(src, original_url)
        for candidate in (canonical_meta, original_url):
            if candidate:
                try:
                    canonical = canonicalize(candidate)
                    break
                except UrlError:
                    continue
    elif media == "text/markdown":
        body, title = _markdown_text(src)
        method = "markdown-strip"
    else:
        body, title, method = src, None, "plain-text"
    text = normalize_text(body)
    if not text:
        raise RejectedImport("no readable text could be extracted")
    title = (title or _first_line_title(text) or name)[:500]
    truncated = len(text) > max_chars
    if truncated:
        text = text[:max_chars]
    if original_url and canonical is None:
        try:
            canonical = canonicalize(original_url)
        except UrlError:
            canonical = None
    return Extracted(
        text=text,
        title=title,
        media_type=media,
        method=method,
        raw_digest=digest(raw),
        content_digest=digest(f"{title}\n\n{text}"),
        raw_bytes=len(raw),
        canonical_url=canonical,
        published_at=published,
        truncated=truncated,
    )


def chunk_text(text: str, *, max_chars: int, max_chunks: int = 5_000) -> list[tuple[int, int, str]]:
    """Greedy paragraph packing into chunks of at most ``max_chars``.

    Every chunk is an exact slice ``text[start:end]`` so stored offsets stay truthful for provenance.
    Paragraphs longer than ``max_chars`` are split at the last space before the limit.
    """
    spans: list[tuple[int, int]] = []
    pos = 0
    for part in text.split("\n\n"):
        s, e = pos, pos + len(part)
        pos = e + 2
        if part.strip():
            spans.append((s, e))
    out: list[tuple[int, int]] = []
    cur: tuple[int, int] | None = None
    for ps, pe in spans:
        while pe - ps > max_chars:
            if cur is not None:
                out.append(cur)
                cur = None
            cut = text.rfind(" ", ps + 1, ps + max_chars)
            if cut <= ps:
                cut = ps + max_chars
            out.append((ps, cut))
            ps = cut
            while ps < pe and text[ps].isspace():
                ps += 1
        if ps >= pe:
            continue
        if cur is None:
            cur = (ps, pe)
        elif pe - cur[0] <= max_chars:
            cur = (cur[0], pe)
        else:
            out.append(cur)
            cur = (ps, pe)
    if cur is not None:
        out.append(cur)
    return [(s, e, text[s:e]) for s, e in out[:max_chunks]]
