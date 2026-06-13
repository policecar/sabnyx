"""Local credential loading for the operator's machine.

The operator keeps provider config in dotenv-style files at the repo root —
``.env`` and/or ``moonshot`` (the latter is exactly how the Kimi/Moonshot key
was handed over: ``MOONSHOT_API_KEY=sk-...``). Both are gitignored.

``load_local_env()`` reads them into the process environment but never
overrides a variable that is already set, so a real environment variable (or
CI secret) always wins over an on-disk file. This is what lets ``sabnyx``
run zero-config for a single user without a secrets manager, while staying
safe to run in an environment that injects its own credentials.
"""

from __future__ import annotations

import os
from pathlib import Path

from .paths import ROOT

# Files checked, in order. Later files do not override earlier ones, and
# nothing overrides a variable already present in the environment.
LOCAL_ENV_FILES = (".env", "moonshot")


def parse_dotenv(text: str) -> dict[str, str]:
    """Parse KEY=VALUE lines. Ignores blanks, ``#`` comments, and a leading
    ``export``. Strips matching single/double quotes around the value."""
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :]
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        if key:
            out[key] = value
    return out


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
