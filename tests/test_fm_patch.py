"""fm.patch_frontmatter and fm._SafeYAML (IDD §6 fm.py, D11, §10a).

A transition must change only the lines it owns. Each fixture below is a
hand-written shape the patcher could plausibly corrupt; the assertion is the
byte diff itself: every line outside the patched keys is copied, the body
is byte-identical, and the result still parses to the input plus exactly
the updates.
"""

from __future__ import annotations

import difflib
from datetime import date
from pathlib import Path

import pytest

from sabnyx import fm

UPDATES = {
    "status": "archived",
    "resolved": date(2026, 10, 24),
    "resolution": "superseded by the scan of 2026-10-24",
    "resolution_kind": "wontfix",
}
PATCHED_KEYS = {"status", "resolved", "resolution", "resolution_kind"}


def _key(line: str) -> str:
    return line.split(":", 1)[0].rstrip("\r")


def _assert_diff_limited(raw: str, out: str, *, status_replaced: bool) -> None:
    """Only patched-key lines are added; at most the old status line goes."""
    a, b = raw.split("\n"), out.split("\n")
    removed, added = [], []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(a=a, b=b, autojunk=False).get_opcodes():
        if op in ("replace", "delete"):
            removed += a[i1:i2]
        if op in ("replace", "insert"):
            added += b[j1:j2]
    assert {_key(line) for line in added} <= PATCHED_KEYS, added
    assert len(added) == len(PATCHED_KEYS), added
    if status_replaced:
        assert len(removed) == 1
        assert _key(removed[0]) == "status"
    else:
        assert removed == []


def _body(text: str) -> str:
    """Everything from the closing fence on, byte for byte."""
    lines = text.split("\n")
    fence = [i for i, line in enumerate(lines) if line.rstrip("\r").strip() == "---"]
    return "\n".join(lines[fence[1] :])  # second fence: the first is the opener


FIXTURES = {
    "block list": (
        "---\ntype: observation\ncategory: contradiction\nobserved: 2026-06-12\n"
        "source_agent: sabnyx-scan\nstatus: pending\ninvolves:\n- alpha.md\n- beta.md\n---\n\n"
        "# Alpha vs beta\n\nbody\n",
        True,
    ),
    "flow list": (
        "---\ntype: observation\ncategory: contradiction\nobserved: 2026-06-12\n"
        "status: pending\ninvolves: [alpha.md, beta.md]\n---\n\n# T\n",
        True,
    ),
    "missing status (legacy default)": (
        "---\ntype: observation\ncategory: gap\nobserved: 2026-06-12\ninvolves: []\n---\n\n# T\n",
        False,
    ),
    "CRLF": (
        "---\r\ntype: observation\r\ncategory: gap\r\nobserved: 2026-06-12\r\n"
        "status: pending\r\ninvolves: []\r\n---\r\n\r\n# T\r\n\r\nbody\r\n",
        True,
    ),
    "'status:' inside a quoted value": (
        "---\ntype: observation\ncategory: friction\nobserved: 2026-06-12\nstatus: pending\n"
        "involves: ['status: archived', \"status: promoted\"]\n---\n\n# T\n",
        True,
    ),
    "'status:' inside a block list item": (
        "---\ntype: observation\ncategory: friction\nobserved: 2026-06-12\nstatus: pending\n"
        "involves:\n- 'status: archived'\n---\n\n# T\n",
        True,
    ),
    "comments and odd spacing": (
        "---\ntype: observation   # meta\ncategory: gap\nobserved: 2026-06-12\n"
        "status:   pending   # open\ninvolves: []\n---\n\n# T\n",
        True,
    ),
    "body starting with ---": (
        "---\ntype: observation\ncategory: gap\nobserved: 2026-06-12\nstatus: pending\n"
        "involves: []\n---\n---\nnot: frontmatter\n---\n\nbody\n",
        True,
    ),
    "no trailing newline": (
        "---\ntype: observation\ncategory: gap\nobserved: 2026-06-12\nstatus: pending\n"
        "involves: []\n---\nbody without newline",
        True,
    ),
    "unicode in the body and frontmatter": (
        "---\ntype: observation\ncategory: gap\nobserved: 2026-06-12\nstatus: pending\n"
        "involves: [知識の庭.md]\n---\n\n# Ünïcödé\n\nline sep and nel\u0085here\n",
        True,
    ),
}


@pytest.mark.parametrize("name", sorted(FIXTURES))
def test_patch_changes_only_the_patched_lines(name):
    raw, has_status = FIXTURES[name]
    out = fm.patch_frontmatter(raw, UPDATES)
    _assert_diff_limited(raw, out, status_replaced=has_status)
    assert _body(out) == _body(raw)
    meta, _ = fm.frontmatter.parse(out, handler=fm._SAFE_YAML)
    assert meta["status"] == "archived"
    assert meta["resolved"] == date(2026, 10, 24)
    assert meta["resolution_kind"] == "wontfix"


def test_patch_preserves_crlf_on_rendered_lines():
    raw, _ = FIXTURES["CRLF"]
    out = fm.patch_frontmatter(raw, UPDATES)
    assert "\n" not in out.replace("\r\n", "")  # no bare LF introduced


def test_missing_status_is_inserted_after_category():
    raw, _ = FIXTURES["missing status (legacy default)"]
    out = fm.patch_frontmatter(raw, UPDATES).split("\n")
    assert out[out.index("category: gap") + 1] == "status: archived"


def test_appended_keys_come_in_fixed_order_before_the_fence():
    raw, _ = FIXTURES["block list"]
    out = fm.patch_frontmatter(raw, UPDATES).split("\n")
    close = out.index("---", 1)
    assert [_key(line) for line in out[close - 3 : close]] == [
        "resolved",
        "resolution",
        "resolution_kind",
    ]


@pytest.mark.parametrize(
    "reason",
    [
        "status: archived",
        "a: b # not a comment",
        "---",
        "...",
        "- looks like a list",
        "'quoted' and \"double\"",
        "trailing colon:",
        "1e3",  # unquoted, re-parses as a str (Demonstrate c5)
        "yes",
        "null",
        "知識の庭",
    ],
)
def test_reasons_with_yaml_syntax_round_trip(reason):
    raw, _ = FIXTURES["block list"]
    out = fm.patch_frontmatter(raw, UPDATES | {"resolution": reason})
    meta, _ = fm.frontmatter.parse(out, handler=fm._SAFE_YAML)
    assert meta["resolution"] == reason
    assert meta["status"] == "archived"


@pytest.mark.parametrize("reason", ["a\nb", "a\u0085b", "a b", "a b"])
def test_values_that_render_multiline_are_refused_by_the_patcher_itself(reason):
    # The schema refuses every break character first (splitlines); the
    # patcher is the second net for the ones whose rendering is unsafe: LF
    # spans lines, NEL/LS/PS are emitted raw (NEL also fails the round-trip).
    raw, _ = FIXTURES["block list"]
    with pytest.raises(fm.PatchError, match="one line|round-trip"):
        fm.patch_frontmatter(raw, UPDATES | {"resolution": reason})


@pytest.mark.parametrize("reason", ["a\rb", "a\vb", "a\fb", "a\r\nb"])
def test_escaped_break_characters_render_as_one_safe_line(reason):
    # CR, VT, FF and CRLF render escaped inside one double-quoted line and
    # round-trip (Demonstrate c5), so the patcher cannot corrupt the file
    # with them. Refusing them is the schema's job (P2), tested in
    # test_observation_schema.py.
    raw, _ = FIXTURES["block list"]
    out = fm.patch_frontmatter(raw, UPDATES | {"resolution": reason})
    _assert_diff_limited(raw, out, status_replaced=True)
    meta, _ = fm.frontmatter.parse(out, handler=fm._SAFE_YAML)
    assert meta["resolution"] == reason


@pytest.mark.parametrize(
    ("raw", "match"),
    [
        ("type: observation\nstatus: pending\n", "open the file"),  # no frontmatter
        ("intro\n---\ntype: observation\nstatus: pending\n---\n", "open the file"),  # text first
        ("---\ntype: observation\nstatus: pending\n", "closing"),
        ("---\ntype: observation\nmeta:\n  nested: 1\nstatus: pending\n---\n", "nested"),
        (
            "---\ntype: observation\ncategory: gap\nstatus: pending\nstatus: pending\n---\n",
            "more than one",
        ),
        ('---\ntype: observation\ncategory: gap\n"status": pending\n---\n', "not on a column-0"),
        ("---\ntype: observation\nobserved: 2026-06-12\n---\n", "no category"),
        (
            "---\ntype: observation\ncategory: gap\nstatus: pending\nresolved: 2026-01-01\n---\n",
            "already present",
        ),
        ("---\ntype: [unclosed\ncategory: gap\n---\n", "malformed YAML"),
    ],
)
def test_unpatchable_inputs_raise(raw, match):
    with pytest.raises(fm.PatchError, match=match):
        fm.patch_frontmatter(raw, UPDATES)


def test_multiline_status_value_is_caught_by_the_reparse_check():
    # A folded scalar spans two lines; replacing only the first would leave
    # an orphaned continuation. The re-parse check refuses the result.
    raw = "---\ntype: observation\ncategory: gap\nstatus: >-\n  pending\ninvolves: []\n---\n"
    with pytest.raises(fm.PatchError, match="re-parse"):
        fm.patch_frontmatter(raw, UPDATES)


def test_only_lifecycle_keys_are_patchable():
    raw, _ = FIXTURES["block list"]
    with pytest.raises(fm.PatchError, match="not patchable"):
        fm.patch_frontmatter(raw, {"involves": []})


# -- _SafeYAML ------------------------------------------------------------------


def test_python_object_tag_is_a_frontmatter_error_and_never_runs(tmp_vault):
    # If the tag were constructed, os.remove would delete the sentinel.
    sentinel = tmp_vault["OBSERVATIONS"].parent / "sentinel.txt"
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    sentinel.write_text("still here", encoding="utf-8")
    p = tmp_vault["OBSERVATIONS"] / "2026-06-12-gap-tag.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        "---\ntype: observation\ncategory: gap\nobserved: 2026-06-12\n"
        f"x: !!python/object/apply:os.remove [{str(sentinel)!r}]\n---\n",
        encoding="utf-8",
    )
    with pytest.raises(fm.FrontmatterError, match="malformed YAML"):
        fm.load(p)
    assert sentinel.read_text(encoding="utf-8") == "still here"


def test_os_system_tag_is_a_frontmatter_error(tmp_vault):
    p = tmp_vault["OBSERVATIONS"] / "2026-06-12-gap-system.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text('---\nx: !!python/object/apply:os.system ["true"]\n---\n', encoding="utf-8")
    with pytest.raises(fm.FrontmatterError, match="malformed YAML"):
        fm.load(p)


def test_fm_load_passes_the_safe_handler_explicitly(tmp_vault, monkeypatch):
    p = tmp_vault["OBSERVATIONS"] / "2026-06-12-gap-h.md"
    fm.write(
        p,
        {"type": "observation", "category": "gap", "observed": "2026-06-12", "involves": []},
        "# h\n",
    )
    seen = []
    real_load = fm.frontmatter.load

    def spy(*args, **kwargs):
        seen.append(kwargs.get("handler"))
        return real_load(*args, **kwargs)

    monkeypatch.setattr(fm.frontmatter, "load", spy)
    fm.load(p)
    assert seen
    assert all(isinstance(h, fm._SafeYAML) for h in seen)


def test_frontmatter_only_counts_at_the_start_of_the_file(tmp_vault):
    # Explicit handlers skip the library's detect() step; _SafeYAML.split
    # keeps its rule. Without it, the valid-looking block between the two
    # horizontal rules below would parse as this file's frontmatter.
    p = tmp_vault["OBSERVATIONS"] / "2026-06-12-gap-late.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        "intro line\n---\ntype: observation\ncategory: gap\nobserved: 2026-06-12\n---\nbody\n",
        encoding="utf-8",
    )
    with pytest.raises(fm.FrontmatterError, match="type"):
        fm.load(p)


def test_toml_header_is_not_auto_detected(tmp_vault):
    p = tmp_vault["OBSERVATIONS"] / "2026-06-12-gap-toml.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        '+++\ntype = "observation"\ncategory = "gap"\nobserved = 2026-06-12\n+++\n',
        encoding="utf-8",
    )
    with pytest.raises(fm.FrontmatterError):
        fm.load(p)


# -- write_text_atomic is the shared primitive ----------------------------------


def test_write_text_atomic_writes_exact_bytes(tmp_path: Path):
    p = tmp_path / "x.md"
    text = "---\r\nstatus: pending\r\n---\r\nbody still body\r\n"
    fm.write_text_atomic(p, text)
    assert p.read_bytes() == text.encode("utf-8")


def test_patch_then_atomic_write_is_byte_exact_outside_patched_lines(tmp_path: Path):
    raw, _ = FIXTURES["CRLF"]
    p = tmp_path / "o.md"
    p.write_bytes(raw.encode("utf-8"))
    fm.write_text_atomic(p, fm.patch_frontmatter(p.read_bytes().decode("utf-8"), UPDATES))
    assert _body(p.read_bytes().decode("utf-8")) == _body(raw)


# -- leading whitespace: the patcher accepts what the loader accepts (P1) ------------

_OBS_KEYS = (
    "type: observation\ncategory: gap\nobserved: 2026-06-12\nstatus: pending\ninvolves: []\n"
)

LEADING = {
    "blank line": "\n---\n" + _OBS_KEYS + "---\n\n# T\n",
    "spaces before the fence": "   ---\n" + _OBS_KEYS + "---\n\n# T\n",
    "CRLF blank line": ("\r\n---\r\n" + _OBS_KEYS.replace("\n", "\r\n") + "---\r\n\r\n# T\r\n"),
}


@pytest.mark.parametrize("name", sorted(LEADING))
def test_leading_whitespace_is_copied_and_the_patch_applies(name):
    raw = LEADING[name]
    out = fm.patch_frontmatter(raw, UPDATES)
    lead = raw[: len(raw) - len(raw.lstrip())]
    assert out.startswith(lead)
    assert out[len(lead) :].startswith("---")
    _assert_diff_limited(raw, out, status_replaced=True)
    assert _body(out) == _body(raw)
    meta, _ = fm.frontmatter.parse(out, handler=fm._SAFE_YAML)
    assert meta["status"] == "archived"


def test_crlf_blank_line_keeps_crlf_on_rendered_lines():
    out = fm.patch_frontmatter(LEADING["CRLF blank line"], UPDATES)
    assert "\n" not in out.replace("\r\n", "")


@pytest.mark.parametrize(
    "raw",
    [
        *LEADING.values(),
        "---\n" + _OBS_KEYS + "---\n",  # the ordinary shape
        "intro\n---\n" + _OBS_KEYS + "---\n",  # text before the fence
        _OBS_KEYS,  # no frontmatter at all
    ],
)
def test_loader_and_patcher_agree_on_which_files_have_frontmatter(tmp_vault, raw):
    # The property behind this fix: a file fm.load counts as an observation
    # is one `resolve` can patch, and a file it rejects the patcher rejects
    # too. Before the fix, a leading blank line was counted but unresolvable.
    p = tmp_vault["OBSERVATIONS"] / "2026-06-12-gap-agree.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(raw.encode("utf-8"))
    try:
        fm.load(p)
        loads = True
    except fm.FrontmatterError:
        loads = False
    try:
        fm.patch_frontmatter(raw, UPDATES)
        patches = True
    except fm.PatchError:
        patches = False
    assert loads == patches
