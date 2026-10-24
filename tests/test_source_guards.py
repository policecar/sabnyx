"""Source-tree guards: properties enforced by grepping src/, not by review.

Each guard pins one boundary from IDD §10a so a future direct call cannot
bypass it silently. The guards scan the real source tree, so a new module
is covered the moment it exists.
"""

from __future__ import annotations

import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "sabnyx"


def _hits(pattern: re.Pattern[str]) -> list[str]:
    out = []
    for p in sorted(SRC.rglob("*.py")):
        for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            if pattern.search(line):
                out.append(f"{p.relative_to(SRC).as_posix()}:{n}: {line.strip()}")
    return out


def _outside(hits: list[str], allowed: str) -> list[str]:
    return [h for h in hits if not h.startswith(f"{allowed}:")]


# -- _SafeYAML at every parse site (§10a) -----------------------------------------

# A direct parse would choose its own handler (the library default, or none).
_FRONTMATTER_PARSE = re.compile(r"\bfrontmatter\.(load|loads|parse)\(")
# Importing the library elsewhere is the first step to such a call, and an
# alias (`import frontmatter as f`) would slip past the call pattern above.
_FRONTMATTER_IMPORT = re.compile(r"^\s*(import\s+frontmatter\b|from\s+frontmatter\b)")
# Loaders that can construct arbitrary objects. Nothing in src/ needs them.
_UNSAFE_YAML = re.compile(
    r"\byaml\.(load|load_all|full_load|full_load_all|unsafe_load|unsafe_load_all)\("
)


def test_frontmatter_is_parsed_only_in_fm():
    assert _outside(_hits(_FRONTMATTER_PARSE), "fm.py") == []


def test_python_frontmatter_is_imported_only_in_fm():
    assert _outside(_hits(_FRONTMATTER_IMPORT), "fm.py") == []


def test_every_frontmatter_parse_in_fm_passes_the_safe_handler():
    calls = _hits(_FRONTMATTER_PARSE)
    assert calls, "guard is vacuous: fm.py must parse frontmatter somewhere"
    assert [c for c in calls if "handler=_SAFE_YAML" not in c] == []


def test_no_unsafe_yaml_loader_anywhere():
    assert _hits(_UNSAFE_YAML) == []


def test_guards_match_what_they_claim():
    # The patterns themselves: a guard that never matches proves nothing.
    assert _FRONTMATTER_PARSE.search("post = frontmatter.load(str(path))")
    assert _FRONTMATTER_PARSE.search("x = frontmatter.loads(text)")
    assert not _FRONTMATTER_PARSE.search("x = fm.load(path)")
    assert _FRONTMATTER_IMPORT.search("import frontmatter")
    assert _FRONTMATTER_IMPORT.search("    import frontmatter as f")
    assert _FRONTMATTER_IMPORT.search("from frontmatter import loads")
    assert not _FRONTMATTER_IMPORT.search("from . import fm")
    assert _UNSAFE_YAML.search("yaml.load(s, Loader=yaml.Loader)")
    assert _UNSAFE_YAML.search("yaml.unsafe_load(s)")
    assert not _UNSAFE_YAML.search("yaml.safe_load(s)")
