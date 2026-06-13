"""Raw sources layer — vault/sources/, the immutable artifacts.

Karpathy's three layers put raw sources (immutable) under the wiki
(LLM-owned). Stage 0 sabnyx skipped this; ADR 0005 adds it. Files in
vault/sources/ are write-once, carry no frontmatter schema, and are
excluded from lint. Knowledge files reference them via the reserved
HTML-comment syntax:  <!-- source-id:sabnyx-sources:<filename> -->
"""

from __future__ import annotations

import urllib.error
import urllib.request
from pathlib import Path

from . import ledger
from .paths import ROOT, SOURCES, ensure_tree
from .schemas import ExtractionDepth
from .util import slugify

# Cap pathological inputs; well under the 1M-token context either way.
MAX_SOURCE_CHARS = 600_000

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


def fetch_url(url: str, timeout: int = 30) -> str:
    """Fetch a URL's raw body as text. No HTML parsing — the model reads
    markup fine, and the raw artifact is the point."""
    req = urllib.request.Request(url, headers={"User-Agent": "sabnyx/0.1 (+knowledge vault)"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            charset = resp.headers.get_content_charset() or "utf-8"
            return resp.read().decode(charset, errors="replace")
    except (urllib.error.URLError, TimeoutError) as e:
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
        raise FileExistsError(f"source already captured: {path.relative_to(ROOT)}")

    header = ""
    if source_url:
        header = f"<!-- source-url: {source_url} -->\n<!-- captured-by: {actor} -->\n\n"
    path.write_text(header + text, encoding="utf-8")

    ledger.append_event(
        kind="source.captured",
        actor=actor,
        subject=str(path.relative_to(ROOT)),
        payload={
            "source_url": source_url,
            "chars": len(text),
            "words": len(text.split()),
        },
    )
    return path
