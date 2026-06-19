"""Raw sources layer — vault/sources/, the immutable artifacts.

Karpathy's three layers put raw sources (immutable) under the wiki
(LLM-owned). Stage 0 sabnyx skipped this; ADR 0005 adds it. Files in
vault/sources/ are write-once, carry no frontmatter schema, and are
excluded from lint. Knowledge files reference them via the reserved
HTML-comment syntax:  <!-- source-id:sabnyx-sources:<filename> -->
"""

from __future__ import annotations

import ipaddress
import socket
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import ledger
from .paths import DATA_ROOT, SOURCES, ensure_tree
from .schemas import ExtractionDepth
from .util import slugify

# Cap pathological inputs; well under the 1M-token context either way.
MAX_SOURCE_CHARS = 600_000

# Hard byte cap on a fetched body: bound memory before we ever decode/truncate
# (a server that lies about Content-Length, or simply serves something huge,
# can't exhaust the single-threaded server).
MAX_FETCH_BYTES = 8 * 1024 * 1024

# Only these schemes are fetchable. No file://, ftp://, gopher://, data:, etc.
_ALLOWED_SCHEMES = frozenset({"http", "https"})

_ARXIV_HINTS = ("arxiv.org", "doi.org", ".pdf")


def classify_depth(text: str, source_url: str | None = None) -> ExtractionDepth:
    """jibot's type-specific extraction depth: classify by word count and
    URL pattern so thin sources get shallow extraction and dense ones deep."""
    if source_url and any(h in source_url.lower() for h in _ARXIV_HINTS):
        return "deep"
    words = len(text.split())
    if words < 500:
        return "shallow"
    if words < 2500:
        return "standard"
    if words < 5000:
        return "standard-plus"
    return "deep"


def _is_blocked_ip(ip: str) -> bool:
    """True for any address we refuse to connect to: loopback, RFC-1918
    private, link-local (incl. cloud metadata 169.254.169.254), reserved,
    multicast, or unspecified."""
    addr = ipaddress.ip_address(ip)
    return (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_reserved
        or addr.is_multicast
        or addr.is_unspecified
    )


def _guard_url(url: str) -> None:
    """Reject a URL that is not http(s) or that resolves to an internal/reserved
    address — the SSRF guard. Raises ValueError on refusal.

    Note: there is a residual TOCTOU/DNS-rebinding window between this check and
    the actual connect (the resolver could return a different address). For a
    single-user tool fetching operator-/sender-supplied URLs this is an
    acceptable risk; closing it fully would require connecting to the validated
    IP with a pinned Host header.
    """
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in _ALLOWED_SCHEMES:
        raise ValueError(f"refusing non-http(s) URL scheme: {parsed.scheme or '(none)'!r}")
    host = parsed.hostname
    if not host:
        raise ValueError(f"URL has no host: {url!r}")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise ValueError(f"could not resolve host {host!r}: {e}") from e
    for info in infos:
        # sockaddr[0] is the address; str() pins it for AF_INET/AF_INET6 (the
        # tuple element type is broad). Always a host string here.
        ip = str(info[4][0])
        if _is_blocked_ip(ip):
            raise ValueError(f"refusing to fetch internal/reserved address {ip} (host {host!r})")


class _GuardedRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Re-run the SSRF guard on every redirect hop so an allow-listed URL can't
    302 into an internal address."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        _guard_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_url(url: str, timeout: int = 30) -> str:
    """Fetch a URL's raw body as text. No HTML parsing — the model reads
    markup fine, and the raw artifact is the point.

    Hardened against SSRF: http(s) only, internal/reserved addresses refused,
    redirects re-validated per hop, and the body capped at MAX_FETCH_BYTES.
    """
    try:
        _guard_url(url)
        opener = urllib.request.build_opener(_GuardedRedirectHandler())
        req = urllib.request.Request(url, headers={"User-Agent": "sabnyx/0.1 (+knowledge vault)"})
        with opener.open(req, timeout=timeout) as resp:  # noqa: S310 — scheme guarded above
            charset = resp.headers.get_content_charset() or "utf-8"
            # read at most MAX_FETCH_BYTES: bounded memory even if the server
            # lies about (or omits) Content-Length.
            raw = resp.read(MAX_FETCH_BYTES)
        return raw.decode(charset, errors="replace")
    except (urllib.error.URLError, TimeoutError, ValueError, socket.gaierror) as e:
        raise RuntimeError(f"could not fetch {url}: {e}") from e


def capture(
    *,
    text: str,
    slug: str,
    actor: str,
    source_url: str | None = None,
    suffix: str = ".md",
) -> Path:
    """Write one immutable raw artifact to vault/sources/ and ledger it.

    Refuses to overwrite: sources are write-once. Returns the path.
    """
    ensure_tree()
    if len(text) > MAX_SOURCE_CHARS:
        text = text[:MAX_SOURCE_CHARS]

    path = SOURCES / f"{slugify(slug)}{suffix}"
    if path.exists():
        raise FileExistsError(f"source already captured: {path.relative_to(DATA_ROOT)}")

    header = ""
    if source_url:
        header = f"<!-- source-url: {source_url} -->\n<!-- captured-by: {actor} -->\n\n"
    path.write_text(header + text, encoding="utf-8")

    ledger.append_event(
        kind="source.captured",
        actor=actor,
        subject=str(path.relative_to(DATA_ROOT)),
        payload={
            "source_url": source_url,
            "chars": len(text),
            "words": len(text.split()),
        },
    )
    return path
