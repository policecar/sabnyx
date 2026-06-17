"""Minimal dotenv parser shared by paths.py and config.py.

Extracted to a dependency-free module so that both ``paths.py`` (which runs at
import time before ``config.load_local_env()`` fires) and ``config.py`` (which
imports ``paths.ROOT``) can use the parser without creating a circular import.

Imports nothing from the sabnyx package — pure stdlib only.
"""

from __future__ import annotations

# Files consulted in order.  Later files do not override earlier ones, and
# nothing overrides a variable already present in os.environ.
LOCAL_ENV_FILES: tuple[str, ...] = (".env",)


def parse_dotenv(text: str) -> dict[str, str]:
    """Parse KEY=VALUE lines.  Ignores blanks, ``#`` comments, and a leading
    ``export``.  Strips matching single/double quotes around the value."""
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):]
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
