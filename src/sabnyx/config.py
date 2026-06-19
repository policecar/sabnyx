"""Local credential loading for the operator's machine.

The operator keeps provider config and secrets (including ``MOONSHOT_API_KEY``)
in a gitignored ``.env`` file at the repo root.

``load_local_env()`` reads it into the process environment but never overrides
a variable that is already set, so a real environment variable (or CI secret)
always wins over an on-disk file. This is what lets ``sabnyx`` run zero-config
for a single user without a secrets manager, while staying safe to run in an
environment that injects its own credentials.
"""

from __future__ import annotations

import os
from pathlib import Path

from .paths import ROOT
from ._dotenv import LOCAL_ENV_FILES, parse_dotenv  # re-exported for callers

# Default trusted source agents for deterministic auto-promote.
_DEFAULT_TRUSTED_SOURCES = {"sabnyx-ingest"}

# Default retrieval implementation behind the Retriever seam (retrieval.py).
_DEFAULT_RETRIEVER = "grep"


def retriever_name() -> str:
    """Return the retriever implementation name behind the Retriever seam.

    Reads ``SABNYX_RETRIEVER`` from the environment; defaults to ``grep``.
    Pure and side-effect-free beyond reading ``os.environ``.
    """
    return os.environ.get("SABNYX_RETRIEVER", "").strip() or _DEFAULT_RETRIEVER


def trusted_sources() -> set[str]:
    """Return the set of source_agent values trusted for auto-promotion.

    Reads ``SABNYX_TRUSTED_SOURCES`` (comma-separated) from the environment.
    If the variable is absent or empty, returns the default ``{"sabnyx-ingest"}``.

    Trust here means "provenance the operator has vetted", NOT "a channel that
    wrote the file". Only operator-supplied local input (``sabnyx-ingest``)
    is trusted by default. Remotely fetched or third-party content is stamped
    with distinct, non-trusted actors (``sabnyx-ingest-web`` for URL ingestion,
    ``sabnyx-telegram``, ``sabnyx-r1``) so prompt-injected content cannot
    auto-promote unattended into the authoritative atlas tier. Add an actor here
    only if you genuinely trust everything that channel produces.

    Pure and side-effect-free beyond reading ``os.environ``.
    """
    raw = os.environ.get("SABNYX_TRUSTED_SOURCES", "").strip()
    if not raw:
        return set(_DEFAULT_TRUSTED_SOURCES)
    return {s.strip() for s in raw.split(",") if s.strip()}


def load_local_env(root: Path | None = None) -> list[str]:
    """Load LOCAL_ENV_FILES from ``root`` into ``os.environ`` via setdefault.

    Returns the names of variables newly set (handy for diagnostics). A
    variable already in the environment is left untouched — explicit always
    beats on-disk.
    """
    root = root or ROOT
    loaded: list[str] = []
    for name in LOCAL_ENV_FILES:
        p = root / name
        if not p.is_file():
            continue
        for key, value in parse_dotenv(p.read_text(encoding="utf-8")).items():
            if key not in os.environ:
                os.environ[key] = value
                loaded.append(key)
    return loaded
