"""Raw sources layer — vault/sources/, the immutable artifacts.

Karpathy's three layers put raw sources (immutable) under the wiki
(LLM-owned). Stage 0 sabnyx skipped this; ADR 0005 adds it. Files in
vault/sources/ are write-once, carry no frontmatter schema, and are
excluded from lint. Knowledge files reference them via the reserved
HTML-comment syntax:  <!-- source-id:sabnyx-sources:<filename> -->
"""

from __future__ import annotations

import contextlib
import hashlib
import ipaddress
import os
import secrets
import socket
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
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


# Carrier-grade NAT (RFC 6598). Not flagged by ipaddress.is_private, but it is
# never a legitimate public destination — treat it like RFC-1918.
_CGNAT_NET = ipaddress.ip_network("100.64.0.0/10")


def _is_blocked_ip(ip: str) -> bool:
    """True for any address we refuse to connect to: loopback, RFC-1918
    private, link-local (incl. cloud metadata 169.254.169.254), CGNAT,
    reserved, multicast, or unspecified."""
    addr = ipaddress.ip_address(ip)
    # is_private/is_loopback/is_link_local already unwrap IPv4-mapped IPv6
    # (::ffff:a.b.c.d) internally, but CGNAT has no stdlib predicate — unwrap
    # it ourselves so an IPv4-mapped CGNAT address isn't missed.
    v4 = addr.ipv4_mapped if isinstance(addr, ipaddress.IPv6Address) else addr
    return (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_reserved
        or addr.is_multicast
        or addr.is_unspecified
        or (v4 is not None and v4.version == 4 and v4 in _CGNAT_NET)
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


# @decision DEC-SRC-001: publish a raw source by hard-linking a complete
#   private temp file to its final name, retrying the next candidate name on
#   EEXIST.
# @rationale: an exists() check followed by a write is a check/write race, and
#   Path.write_text truncates whatever another process published in between.
#   CLI ingest does not hold the daemon's vault lock, so a lock-based fix would
#   have to add a second, nested lock to every capture path. os.link(2) instead
#   creates the name only if it is absent, atomically, across processes, and
#   the name appears with its bytes already complete — so a contender never
#   sees a partial file and mistakes it for a reusable capture. os.replace or
#   rename would silently overwrite; O_EXCL + write would expose a partial
#   file. Interruption limits: a crash can leave a hidden `.<slug>.*.tmp` in
#   sources/ (never a partial published name); a crash after the link and
#   before the ledger append leaves a complete artifact with no
#   `source.captured` event (no file-plus-ledger transaction is claimed).


def _source_candidates(base: str, digest: str, suffix: str) -> Iterator[Path]:
    """Every name a capture of this content may publish under, in order.

    The plain slug first, then content-hash suffixes widened 8 hex digits at a
    time (a short-hash collision with different content moves to a longer
    one), then, at full width, numeric serials as a defensive fallback (e.g.
    if a file was manually replaced).
    """
    yield SOURCES / f"{base}{suffix}"
    for width in range(8, len(digest) + 1, 8):
        yield SOURCES / f"{base}-{digest[:width]}{suffix}"
    serial = 2
    while True:
        yield SOURCES / f"{base}-{digest}-{serial}{suffix}"
        serial += 1


def _write_private_temp(directory: Path, stem: str, data: bytes) -> Path:
    """Write `data` to a fresh hidden file in `directory` and fsync it.

    The name is unique to this call (pid + random token, created O_EXCL), so
    nothing else ever writes to it and only this capture may delete it. Mode
    0o666 lets the kernel apply the umask, as a plain write would; a
    tempfile.mkstemp file would be 0600 (see fm.write_text_atomic).
    """
    tmp = directory / f".{stem}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view) :]
        os.fsync(fd)
    except BaseException:
        os.close(fd)
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise
    os.close(fd)
    return tmp


def _fsync_dir(directory: Path) -> None:
    """Best-effort durability for a new directory entry (as fm does)."""
    with contextlib.suppress(OSError):
        dfd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)


def capture(
    *,
    text: str,
    slug: str,
    actor: str,
    source_url: str | None = None,
    suffix: str = ".md",
    reuse_existing: bool = False,
) -> Path:
    """Write one immutable raw artifact to vault/sources/ and ledger it.

    Write-once per *content*: recapturing identical content raises
    FileExistsError unless ``reuse_existing`` is true, in which case the
    retained artifact is returned for re-extraction. A slug collision with
    DIFFERENT stored content (including provenance headers) gets a content-hash
    suffix instead of a refusal. Returns the path.

    Safe against concurrent captures in other processes (DEC-SRC-001): no
    capture ever truncates or replaces a published source, and every published
    source is complete. The `source.captured` event is appended only after this
    call published a new artifact; reuse appends nothing.
    """
    ensure_tree()
    if len(text) > MAX_SOURCE_CHARS:
        text = text[:MAX_SOURCE_CHARS]

    header = ""
    if source_url:
        header = f"<!-- source-url: {source_url} -->\n<!-- captured-by: {actor} -->\n\n"
    content = header + text
    data = content.encode("utf-8")

    base = slugify(slug)
    # Provenance is part of the stored artifact, so hash the complete bytes,
    # not only the raw text.
    digest = hashlib.sha256(data).hexdigest()
    tmp = _write_private_temp(SOURCES, base, data)
    try:
        for candidate in _source_candidates(base, digest, suffix):
            try:
                os.link(tmp, candidate)
            except FileExistsError:
                # Published names only ever appear complete (via link), so
                # these bytes are a whole artifact, never a half-written one.
                if candidate.read_bytes() != data:
                    continue
                if reuse_existing:
                    return candidate
                raise FileExistsError(
                    f"source already captured: {candidate.relative_to(DATA_ROOT)}"
                ) from None
            path = candidate
            break
    finally:
        # Only ever our own private temp: never a published name, so a failed
        # or interrupted capture cannot delete another writer's artifact.
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
    _fsync_dir(SOURCES)

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
