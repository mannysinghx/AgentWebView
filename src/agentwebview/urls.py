"""URL canonicalization for provenance, deduplication and scope checks.

Only clearly non-semantic variation is normalised: scheme/host case, default ports, IDNA hosts,
dot segments, fragment removal, percent-encoding case, and well-known tracking parameters. Query
parameter order and all other parameters are preserved, so semantically different URLs are never
merged.
"""

from __future__ import annotations

import posixpath
import re
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlsplit, urlunsplit

TRACKING_PARAMS = frozenset(
    {
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "utm_term",
        "utm_content",
        "utm_id",
        "gclid",
        "fbclid",
        "mc_cid",
        "mc_eid",
        "msclkid",
        "yclid",
        "_hsenc",
        "_hsmi",
        "igshid",
    }
)
_SAFE_PATH = "/!$&'()*+,;=:@-._~%"


class UrlError(ValueError):
    pass


def _norm_host(host: str) -> str:
    host = host.strip().rstrip(".").lower()
    if not host:
        raise UrlError("URL has no host")
    if host.startswith("[") and host.endswith("]"):
        return host
    try:
        return host.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise UrlError(f"invalid host: {host!r}") from exc


def canonicalize(url: str, *, max_length: int = 2048) -> str:
    if len(url) > max_length:
        raise UrlError("URL is too long")
    try:
        parts = urlsplit(url.strip())
    except ValueError as exc:
        raise UrlError(f"unparseable URL: {exc}") from exc
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        raise UrlError(f"unsupported scheme {scheme!r}; only http and https are allowed")
    if parts.username or parts.password:
        raise UrlError("URLs with embedded credentials are not allowed")
    host = _norm_host(parts.hostname or "")
    try:
        port = parts.port
    except ValueError as exc:
        raise UrlError("invalid port") from exc
    netloc = host if port is None or (scheme, port) in (("http", 80), ("https", 443)) else f"{host}:{port}"
    path = parts.path or "/"
    trailing = path.endswith("/")
    path = posixpath.normpath(unquote(path)) if path not in ("", "/") else "/"
    if path == ".":
        path = "/"
    if trailing and not path.endswith("/"):
        path += "/"
    path = quote(path, safe=_SAFE_PATH)
    pairs = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k.lower() not in TRACKING_PARAMS]
    query = urlencode(pairs, quote_via=quote, safe="/:@-._~!$'()*+,;=")
    return urlunsplit((scheme, netloc, path, query, ""))


def host_of(url: str) -> str:
    return _norm_host(urlsplit(url).hostname or "")


_HOST_RE = re.compile(r"^(?=.{1,253}$)([a-z0-9-]{1,63}\.)*[a-z0-9-]{1,63}$")


def valid_host_pattern(host: str) -> bool:
    return bool(_HOST_RE.match(host.lower().rstrip(".")))
