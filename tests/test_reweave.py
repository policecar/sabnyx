"""Tests for marker blocks, pending-intent resolution, and the reweave command."""

from __future__ import annotations

import pytest
from click.testing import CliRunner

from sabnyx import fm, ledger, util
from sabnyx.schemas import ProposedObservation, RelatedAddition, ReweaveChangeset

# -- marker blocks -----------------------------------------------------------------


def test_marker_block_roundtrip():
    body = "intro\n\n## Related\n\n<!-- BEGIN sabnyx:related -->\n<!-- END sabnyx:related -->\n"
    assert util.get_marker_block(body, "related") == ""
    updated = util.set_marker_block(body, "related", ["- [[a]]", "- [[b]]"])
    assert util.get_marker_block(updated, "related") == "- [[a]]\n- [[b]]"
    # everything outside the block is preserved byte-for-byte
    assert updated.startswith("intro\n\n## Related\n\n")
    assert updated.endswith("<!-- END sabnyx:related -->\n")
    # idempotent replace, not append
    again = util.set_marker_block(updated, "related", ["- [[a]]"])
    assert util.get_marker_block(again, "related") == "- [[a]]"
    assert again.count("BEGIN sabnyx:related") == 1


def test_marker_block_appended_when_missing():
    body = "# Title\n\nprose only\n"
    updated = util.set_marker_block(body, "related", ["- [[x]]"], heading="Related")
    assert updated.startswith("# Title\n\nprose only\n")
    assert "## Related" in updated
    assert util.get_marker_block(updated, "related") == "- [[x]]"


# -- vault scaffolding ----------------------------------------------------------------


def _make_atlas_file(dir_, slug, description, body="", related=True):
    meta = {
        "type": "concept",
        "description": description,
        "tags": [],
        "status": "final",
        "promoted_from": None,
        "promoted_date": "2026-06-12",
        "last_verified": "2026-06-12",
        "proof": None,
    }
    block = "\n\n## Related\n\n<!-- BEGIN sabnyx:related -->\n<!-- END sabnyx:related -->\n"
    fm.write(dir_ / f"{slug}.md", meta, f"# {slug}\n\n{body}" + (block if related else "\n"))
    return dir_ / f"{slug}.md"


def _propose_reweave(rel_subject):
    ledger.append_event(
        plane="intent",
        kind="reweave.proposed",
        actor="sabnyx-cli",
        subject=rel_subject,
        payload={"prompt": "..."},
    )


# -- pending resolution ------------------------------------------------------------------


def test_pending_targets_consumed_by_completion(tmp_vault):
    from sabnyx.commands import reweave as rw

    target = _make_atlas_file(
        tmp_vault["ATLAS_CONCEPTS"], "alpha", "a concept about alpha things, long enough to pass"
    )
    rel = str(target.relative_to(tmp_vault["DATA_ROOT"]))
    _propose_reweave(rel)
    assert [str(p) for p in rw._pending_targets()] == [str(target)]

    ledger.append_event(kind="reweave.completed", actor="t", subject=rel, payload={})
    assert rw._pending_targets() == []


def test_pending_targets_skips_deleted_files(tmp_vault):
    from sabnyx.commands import reweave as rw

    _propose_reweave("vault/atlas/concepts/gone.md")
    assert rw._pending_targets() == []


def test_pending_targets_requeue_after_completion(tmp_vault):
    """Last event wins per subject: a historical completion must not mask a
    later intent (delete → re-ingest → re-promote re-queues)."""
    from sabnyx.commands import reweave as rw

    target = _make_atlas_file(
        tmp_vault["ATLAS_CONCEPTS"], "alpha", "a concept about alpha things, long enough to pass"
    )
    rel = str(target.relative_to(tmp_vault["DATA_ROOT"]))
    _propose_reweave(rel)
    ledger.append_event(kind="reweave.completed", actor="t", subject=rel, payload={})
    _propose_reweave(rel)  # the file was re-promoted: a fresh intent
    assert [str(p) for p in rw._pending_targets()] == [str(target)]


# -- end-to-end (faked LLM) -----------------------------------------------------------------


CANNED = ReweaveChangeset(
    summary="alpha bridges to beta; gamma contradiction noted.",
    related_additions=[
        # backward link: beta gains a link to the target
        RelatedAddition(file="beta", add_links=["alpha"], reason="same topic", priority="bridge"),
        # outbound link on the target itself + guards: self-link, unknown slug
        RelatedAddition(
            file="alpha",
            add_links=["beta", "alpha", "does-not-exist"],
            reason="bidirectional",
        ),
        # duplicate: beta already links alpha after first addition? (separate file: gamma
        # already links beta in prose, so the add should be dropped)
        RelatedAddition(file="gamma", add_links=["beta"], reason="dup test"),
    ],
    observations=[
        ProposedObservation(
            category="contradiction",
            title="alpha vs gamma claim",
            body="alpha says X, gamma says not-X. Suggested resolution: verify source.",
            involves=["alpha.md", "gamma.md"],
        )
    ],
)


def _run_reweave(monkeypatch, args, input_text=None):
    from sabnyx.commands import reweave as rw

    calls: list[dict] = []

    def fake_parse_structured(**kwargs):
        calls.append(kwargs)
        return CANNED

    monkeypatch.setattr(rw.llm, "parse_structured", fake_parse_structured)
    runner = CliRunner()
    result = runner.invoke(rw.reweave_cmd, args, input=input_text)
    return result, calls


def test_reweave_pending_applies_guarded_changeset(tmp_vault, monkeypatch):
    concepts = tmp_vault["ATLAS_CONCEPTS"]
    target = _make_atlas_file(concepts, "alpha", "a concept about alpha things, long enough yes")
    _make_atlas_file(concepts, "beta", "a concept about beta things, also long enough to pass")
    _make_atlas_file(
        concepts,
        "gamma",
        "a concept about gamma things, long enough to pass too",
        body="prose already links [[beta]] here.\n",
    )
    rel = str(target.relative_to(tmp_vault["DATA_ROOT"]))
    _propose_reweave(rel)

    result, calls = _run_reweave(monkeypatch, ["--yes"])
    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    assert calls[0]["role"] == "standard"

    # beta gained the backward link
    _, beta_body, _ = fm.load(concepts / "beta.md")
    assert "[[alpha]]" in (util.get_marker_block(beta_body, "related") or "")
    # target gained only the legit outbound link (self + unknown dropped)
    _, alpha_body, _ = fm.load(concepts / "alpha.md")
    assert util.get_marker_block(alpha_body, "related") == "- [[beta]]"
    # gamma untouched: link already present in prose
    _, gamma_body, _ = fm.load(concepts / "gamma.md")
    assert util.get_marker_block(gamma_body, "related") == ""

    # contradiction observation written
    obs = list(tmp_vault["OBSERVATIONS"].glob("*-contradiction-*.md"))
    assert len(obs) == 1
    meta, _, _ = fm.load(obs[0])
    assert meta["involves"] == ["alpha.md", "gamma.md"]

    # ledger: intent consumed, edits recorded
    kinds = [e.kind for e in ledger.replay()]
    assert kinds.count("atlas.reweaved") == 2
    assert kinds[-1] == "reweave.completed"

    # second run: nothing pending
    result2, calls2 = _run_reweave(monkeypatch, [])
    assert result2.exit_code == 0
    assert calls2 == []
    assert "nothing to do" in result2.output


def test_reweave_observation_records_producer_reweave(tmp_vault, monkeypatch):
    from sabnyx import observations

    concepts = tmp_vault["ATLAS_CONCEPTS"]
    target = _make_atlas_file(concepts, "alpha", "a concept about alpha things, long enough yes")
    _make_atlas_file(concepts, "beta", "a concept about beta things, also long enough to pass")
    _make_atlas_file(concepts, "gamma", "a concept about gamma things, long enough to pass too")
    _propose_reweave(str(target.relative_to(tmp_vault["DATA_ROOT"])))

    result, _ = _run_reweave(monkeypatch, ["--yes"])
    assert result.exit_code == 0, result.output
    (o,) = observations.observations("all")
    assert o.fm.producer == "reweave"
    assert o.fm.source_agent == "sabnyx-reweave"
    (ev,) = [e for e in ledger.replay() if e.kind == "observation.created"]
    assert ev.subject == o.rel
    assert ev.payload["producer"] == "reweave"
    (done,) = [e for e in ledger.replay() if e.kind == "reweave.completed"]
    assert done.payload["observations"] == [o.rel]


def test_reweave_dry_run_writes_nothing(tmp_vault, monkeypatch):
    concepts = tmp_vault["ATLAS_CONCEPTS"]
    target = _make_atlas_file(concepts, "alpha", "a concept about alpha things, long enough yes")
    _make_atlas_file(concepts, "beta", "a concept about beta things, also long enough to pass")
    _make_atlas_file(concepts, "gamma", "a concept about gamma things, long enough to pass too")
    _propose_reweave(str(target.relative_to(tmp_vault["DATA_ROOT"])))

    result, calls = _run_reweave(monkeypatch, ["--dry-run"])
    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    _, beta_body, _ = fm.load(concepts / "beta.md")
    assert util.get_marker_block(beta_body, "related") == ""
    # intent NOT consumed on dry run
    from sabnyx.commands import reweave as rw

    assert len(rw._pending_targets()) == 1


def test_apply_links_dedups_within_one_addition(tmp_vault, monkeypatch):
    """The same slug twice in ONE add_links list must yield one line."""
    from sabnyx.commands import reweave as rw

    concepts = tmp_vault["ATLAS_CONCEPTS"]
    target = _make_atlas_file(concepts, "alpha", "a concept about alpha things, long enough yes")
    _make_atlas_file(concepts, "beta", "a concept about beta things, also long enough to pass")
    _propose_reweave(str(target.relative_to(tmp_vault["DATA_ROOT"])))

    changeset = ReweaveChangeset(
        summary="dup within one list",
        related_additions=[RelatedAddition(file="alpha", add_links=["beta", "beta"], reason="dup")],
        observations=[],
    )
    monkeypatch.setattr(rw.llm, "parse_structured", lambda **kw: changeset)
    result = CliRunner().invoke(rw.reweave_cmd, ["--yes"])
    assert result.exit_code == 0, result.output

    _, body, _ = fm.load(concepts / "alpha.md")
    assert util.get_marker_block(body, "related") == "- [[beta]]"
    assert body.count("- [[beta]]") == 1


def test_apply_links_dedup_reported_in_drop_message(tmp_vault, monkeypatch):
    """A literal duplicate in add_links (["beta", "beta"]) must show up in
    the console's dropped-links message, not vanish silently. Regression:
    `dropped = [l for l in add_links if l not in new_links]` is a membership
    check, so the second "beta" -- which IS in new_links -- was invisible to
    the drop tally even though it was never applied."""
    from sabnyx.commands import reweave as rw

    concepts = tmp_vault["ATLAS_CONCEPTS"]
    target = _make_atlas_file(concepts, "alpha", "a concept about alpha things, long enough yes")
    _make_atlas_file(concepts, "beta", "a concept about beta things, also long enough to pass")
    _propose_reweave(str(target.relative_to(tmp_vault["DATA_ROOT"])))

    changeset = ReweaveChangeset(
        summary="dup reporting",
        related_additions=[RelatedAddition(file="alpha", add_links=["beta", "beta"], reason="dup")],
        observations=[],
    )
    monkeypatch.setattr(rw.llm, "parse_structured", lambda **kw: changeset)
    result = CliRunner().invoke(rw.reweave_cmd, ["--yes"])
    assert result.exit_code == 0, result.output
    assert "drop (unknown/self/duplicate): beta" in result.output


def test_apply_links_skips_half_open_marker_block(tmp_vault, monkeypatch):
    """A file with only one of BEGIN/END (manual edit damage) is reported and
    skipped — never given a duplicate/nested block (SPEC §8)."""
    from sabnyx.commands import reweave as rw

    concepts = tmp_vault["ATLAS_CONCEPTS"]
    target = _make_atlas_file(concepts, "alpha", "a concept about alpha things, long enough yes")
    corrupt = concepts / "beta.md"
    fm.write(
        corrupt,
        {
            "type": "concept",
            "description": "a concept about beta things, also long enough to pass",
            "tags": [],
            "status": "final",
            "promoted_from": None,
            "promoted_date": None,
            "last_verified": None,
            "proof": None,
        },
        "# beta\n\n<!-- BEGIN sabnyx:related -->\n- [[old]]\n",  # END lost to a manual edit
    )
    _propose_reweave(str(target.relative_to(tmp_vault["DATA_ROOT"])))

    changeset = ReweaveChangeset(
        summary="link into a corrupt file",
        related_additions=[RelatedAddition(file="beta", add_links=["alpha"], reason="x")],
        observations=[],
    )
    monkeypatch.setattr(rw.llm, "parse_structured", lambda **kw: changeset)
    result = CliRunner().invoke(rw.reweave_cmd, ["--yes"])
    assert result.exit_code == 0, result.output
    assert "corrupt marker block" in result.output
    # the file is untouched: still exactly one BEGIN, no END appended
    text = corrupt.read_text()
    assert text.count("BEGIN sabnyx:related") == 1
    assert "END sabnyx:related" not in text


def test_reweave_explicit_target_rejects_non_atlas(tmp_vault, monkeypatch, tmp_path):
    from sabnyx.commands import reweave as rw

    stray = tmp_path / "stray.md"
    stray.write_text("not atlas")
    runner = CliRunner()
    result = runner.invoke(rw.reweave_cmd, [str(stray)])
    assert result.exit_code == 2
    assert "not under vault/atlas/" in " ".join(result.output.split())


# -- byte preservation outside the owned marker region (R03) ---------------------
#
# Reweave may write marker blocks only (AGENTS §2, SPEC §8). These fixtures
# are complete raw files with every feature a parse/serialize round trip
# normalizes: a YAML comment, quoting, CRLF, an indented first body line (a
# Markdown code block a strip would turn into prose), trailing spaces, prose
# after the block and trailing blank lines. Each test drives the real
# _apply_links and compares the whole file on disk as bytes.

BEGIN, END = "<!-- BEGIN sabnyx:related -->", "<!-- END sabnyx:related -->"

_RAW_HEAD = (
    "---\n"
    "# hand-written metadata comment, kept verbatim\n"
    "type: concept\n"
    "status: 'final'\n"
    'description: "a concept about alpha things, quoted and long enough"\n'
    "tags: [\"kept\",   'quoted']\n"
    "---\n"
    "\n"
    "    code starts with intentional indentation\n"
    "\n"
    "prose with trailing spaces   \n"
)
NEWLINES = pytest.mark.parametrize("nl", ["\n", "\r\n"], ids=["lf", "crlf"])


def _write_raw(path, text, nl):
    path.write_bytes(text.replace("\n", nl).encode("utf-8"))
    return path.read_bytes()


def _alpha_and_beta(tmp_vault, raw_text, nl):
    concepts = tmp_vault["ATLAS_CONCEPTS"]
    alpha = concepts / "alpha.md"
    before = _write_raw(alpha, raw_text, nl)
    _make_atlas_file(concepts, "beta", "a concept about beta things, also long enough to pass")
    return alpha, before


def _apply(target, links, *, file="alpha"):
    from rich.console import Console

    from sabnyx import context, paths
    from sabnyx.commands import reweave as rw

    console = Console(record=True, width=200)
    changeset = ReweaveChangeset(
        summary="link",
        related_additions=[RelatedAddition(file=file, add_links=links, reason="related")],
        observations=[],
    )
    modified = rw._apply_links(target, changeset, context.slug_index(paths.ATLAS), console)
    return modified, console.export_text()


def _reweaved():
    return [e for e in ledger.replay() if e.kind == "atlas.reweaved"]


@NEWLINES
def test_apply_links_preserves_every_byte_outside_the_owned_region(tmp_vault, nl):
    raw = _RAW_HEAD + f"\n## Related\n\n{BEGIN}\n{END}\n\nEnd.   \n\n\n"
    alpha, before = _alpha_and_beta(tmp_vault, raw, nl)

    modified, _ = _apply(alpha, ["beta"])

    # only the owned interior changes, rendered in the file's own newline
    expected = before.replace(
        f"{BEGIN}{nl}{END}".encode(), f"{BEGIN}{nl}- [[beta]]{nl}{END}".encode()
    )
    assert expected != before
    assert alpha.read_bytes() == expected
    rel = str(alpha.relative_to(tmp_vault["DATA_ROOT"]))
    assert modified == [rel]
    (ev,) = _reweaved()
    assert ev.subject == rel
    assert ev.payload["added_links"] == ["beta"]


@NEWLINES
def test_apply_links_keeps_existing_entries_and_skips_identical_repeat(tmp_vault, monkeypatch, nl):
    """Existing block lines are kept (blank lines inside dropped, as before);
    an identical repeat writes nothing and emits no event."""
    raw = _RAW_HEAD + f"\n{BEGIN}\n  - [[old-link]]  \n\n{END}\nafter\n"
    alpha, before = _alpha_and_beta(tmp_vault, raw, nl)

    _apply(alpha, ["beta"])
    once = alpha.read_bytes()
    assert once == before.replace(
        f"{BEGIN}{nl}  - [[old-link]]  {nl}{nl}{END}".encode(),
        f"{BEGIN}{nl}  - [[old-link]]  {nl}- [[beta]]{nl}{END}".encode(),
    )

    writes: list = []
    monkeypatch.setattr(fm, "write_text_atomic", lambda *a: writes.append(a))
    modified, output = _apply(alpha, ["beta", "BETA"])
    assert modified == []
    assert writes == []
    assert alpha.read_bytes() == once
    assert len(_reweaved()) == 1
    assert "drop (unknown/self/duplicate): beta, BETA" in output


@NEWLINES
@pytest.mark.parametrize(
    ("tail", "sep"),
    [("End.", "\n\n"), ("End.\n", "\n"), ("End.\n\n", ""), ("End.\n\n\n", "")],
    ids=["no-newline", "one-newline", "blank-line", "two-blank-lines"],
)
def test_apply_links_appends_related_section_when_block_missing(tmp_vault, nl, tail, sep):
    """Insertion policy: the existing file is a byte-exact prefix; only the
    separator needed to end in one blank line, then the documented section."""
    alpha, before = _alpha_and_beta(tmp_vault, _RAW_HEAD + tail, nl)

    modified, _ = _apply(alpha, ["beta"])

    section = f"{sep}## Related\n\n{BEGIN}\n- [[beta]]\n{END}\n".replace("\n", nl)
    assert alpha.read_bytes() == before + section.encode()
    assert len(modified) == 1


def test_apply_links_ignores_marker_text_inside_frontmatter(tmp_vault):
    """Markers are looked up in the body only: marker text in a frontmatter
    value is neither edited nor counted as a pair."""
    raw = (
        f'---\ntype: concept\ndescription: "documents the {BEGIN} syntax for agents"\n---\n\nbody\n'
    )
    alpha, before = _alpha_and_beta(tmp_vault, raw, "\n")

    _apply(alpha, ["beta"])

    assert alpha.read_bytes() == before + f"\n## Related\n\n{BEGIN}\n- [[beta]]\n{END}\n".encode()


@NEWLINES
@pytest.mark.parametrize(
    "corrupt",
    [
        f"{BEGIN}\n- [[old]]\n",  # half-open: END lost
        f"- [[old]]\n{END}\n",  # half-open: BEGIN lost
        f"{END}\nmiddle prose\n{BEGIN}\n",  # inverted pair
        f"{BEGIN}\n{END}\n\n{BEGIN}\n{END}\n",  # two blocks
        f"{BEGIN}\n{BEGIN}\n{END}\n",  # nested BEGIN
    ],
    ids=["no-end", "no-begin", "end-first", "two-blocks", "nested"],
)
def test_apply_links_refuses_malformed_marker_blocks(tmp_vault, nl, corrupt):
    alpha, before = _alpha_and_beta(tmp_vault, _RAW_HEAD + "\n" + corrupt, nl)

    modified, output = _apply(alpha, ["beta"])

    assert "corrupt marker block" in output
    assert alpha.read_bytes() == before
    assert modified == []
    assert _reweaved() == []


@NEWLINES
def test_apply_links_never_rewrites_invalid_frontmatter(tmp_vault, nl):
    """Invalid metadata is skipped, not cleaned up by a re-serialization."""
    raw = _RAW_HEAD.replace("type: concept", "type: not-a-type") + f"\n{BEGIN}\n{END}\n"
    alpha, before = _alpha_and_beta(tmp_vault, raw, nl)

    modified, output = _apply(alpha, ["beta"])

    assert "invalid frontmatter" in output
    assert alpha.read_bytes() == before
    assert modified == []
    assert _reweaved() == []


def test_apply_links_keeps_file_mode(tmp_vault):
    """The established atomic writer's mode preservation still applies."""
    import os
    import stat

    alpha, _ = _alpha_and_beta(tmp_vault, _RAW_HEAD + f"\n{BEGIN}\n{END}\n", "\n")
    os.chmod(alpha, 0o600)

    _apply(alpha, ["beta"])

    assert stat.S_IMODE(alpha.stat().st_mode) == 0o600
    assert b"- [[beta]]" in alpha.read_bytes()
