"""Behavioral coverage for ROOT != DATA_ROOT: the class of bug that crashed
in production when vault/ledger paths were relativized against ROOT (the
code-repo root) instead of DATA_ROOT (the data-repo root).

These tests run under the updated tmp_vault fixture where ROOT is the real
code repo (~/.../sabnyx) and DATA_ROOT is a fresh temp dir.  Any
path.relative_to(ROOT) call on a vault/ledger file raises ValueError,
surfacing the bug category before it reaches the server.

Coverage targets (previously uncovered crash lines):
  - ingest/serve: src_rel of a captured source (vault/sources/)
  - new: ledger subject for an intake file
  - promote: atlas dest display
  - triage: intake list + pending.md subject + write display
  - health: broken-wikilink display + orphan display + schema-drift display
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from click.testing import CliRunner

from sabnyx import fm, ledger
from sabnyx.schemas import IngestChangeset, ProposedIntake

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _good_meta(
    intake_type: str = "bookmark",
    *,
    description: str = "a long enough description that satisfies the 30 char minimum",
    source_agent: str = "sabnyx-ingest",
) -> dict:
    return {
        "type": intake_type,
        "description": description,
        "source": "web",
        "status": "draft",
        "created": datetime.now(UTC).isoformat(),
        "source_agent": source_agent,
    }


def _make_intake(intake_dir: Path, slug: str, meta: dict, body: str = "# body\n\ncontent\n") -> Path:
    p = intake_dir / f"{slug}.md"
    fm.write(p, meta, body)
    return p


# ---------------------------------------------------------------------------
# Fixture sanity — confirm ROOT != DATA_ROOT
# ---------------------------------------------------------------------------


def test_fixture_root_not_equal_data_root(tmp_vault) -> None:
    """The fixture must produce ROOT != DATA_ROOT, or no crash-class tests are meaningful."""
    from sabnyx import paths as paths_mod

    assert tmp_vault["DATA_ROOT"] != paths_mod.ROOT, (
        "ROOT should be the real code repo, not the temp vault"
    )
    assert tmp_vault["ROOT"] == paths_mod.ROOT


# ---------------------------------------------------------------------------
# ingest: src_rel of a captured source must not crash with ROOT != DATA_ROOT
# ---------------------------------------------------------------------------

CANNED_INGEST = IngestChangeset(
    summary="test ingest.",
    intake_files=[
        ProposedIntake(
            type="thought",
            title="Root Split Thought",
            description="A thought about the root split that is long enough for the schema.",
            tags=[],
            body="Content.",
        )
    ],
    observations=[],
)


def test_ingest_src_rel_uses_data_root(tmp_vault, monkeypatch) -> None:
    """ingest must relativize vault/sources/ path against DATA_ROOT, not ROOT.

    Before the fix, src_path.relative_to(ROOT) crashed with ValueError when
    ROOT != DATA_ROOT because the source file lives under DATA_ROOT.
    """
    from sabnyx.commands import ingest as ingest_mod

    monkeypatch.setattr(ingest_mod.llm, "parse_structured", lambda **kw: CANNED_INGEST)
    result = CliRunner().invoke(
        ingest_mod.ingest_cmd,
        ["--stdin", "--title", "Root Split Source", "--yes"],
        input="some text about the root split\n",
    )
    assert result.exit_code == 0, result.output
    # vault/sources/root-split-source.md must be captured
    assert (tmp_vault["SOURCES"] / "root-split-source.md").exists()
    # The src_rel in output must be a relative path under vault/, not absolute
    assert "vault/sources/" in result.output


# ---------------------------------------------------------------------------
# serve: src_rel of an ingested source via Telegram
# ---------------------------------------------------------------------------


def test_serve_ingest_src_rel_uses_data_root(tmp_vault, monkeypatch) -> None:
    """serve._ingest must relativize vault/sources/ against DATA_ROOT.

    This is the exact crash from the bug report:
      '/home/USER/sabnyx-vault/vault/sources/start.md' is not in the subpath of
      '/home/USER/sabnyx'.
    """
    from sabnyx.commands import serve as serve_mod

    monkeypatch.setattr(serve_mod, "telegram_fetch", lambda url: "fetched content")

    llm_calls: list = []

    def fake_parse_structured(**kwargs):
        llm_calls.append(kwargs)
        return CANNED_INGEST

    import sabnyx.llm as llm_mod

    monkeypatch.setattr(llm_mod, "parse_structured", fake_parse_structured)

    # _ingest is the crash site; call it directly
    reply = serve_mod._ingest(
        "some telegram text", title="telegram-msg", source_kind="manual", source_url=None
    )
    # Must not crash; reply is a string
    assert isinstance(reply, str)
    assert (tmp_vault["SOURCES"] / "telegram-msg.md").exists()


# ---------------------------------------------------------------------------
# new: ledger subject uses DATA_ROOT, not ROOT
# ---------------------------------------------------------------------------


def test_new_ledger_subject_uses_data_root(tmp_vault, monkeypatch) -> None:
    """sabnyx new must record the ledger subject relative to DATA_ROOT.

    Before the fix, path.relative_to(ROOT) for an INTAKE path raised ValueError.
    """
    from sabnyx.commands import new as new_mod

    # Patch DATA_ROOT in new_mod (it imports DATA_ROOT at module level now)
    monkeypatch.setattr(new_mod, "DATA_ROOT", tmp_vault["DATA_ROOT"])
    monkeypatch.setattr(new_mod, "INTAKE", tmp_vault["INTAKE"])
    monkeypatch.setattr(new_mod, "OBSERVATIONS", tmp_vault["OBSERVATIONS"])
    monkeypatch.setattr(new_mod, "ensure_tree", lambda: None)

    result = CliRunner().invoke(
        new_mod.new_cmd,
        [
            "--type", "thought",
            "--title", "Root Split Idea",
            "--description", "a description that is definitely at least 30 chars long yes",
        ],
    )
    assert result.exit_code == 0, result.output

    events = list(ledger.replay())
    assert any(e.kind == "intake.created" for e in events)
    subj = next(e.subject for e in events if e.kind == "intake.created")
    # subject must be a relative path under vault/intake/, not absolute
    assert subj.startswith("vault/intake/")
    assert not Path(subj).is_absolute()


# ---------------------------------------------------------------------------
# promote: atlas dest display uses DATA_ROOT
# ---------------------------------------------------------------------------


def test_promote_display_uses_data_root(tmp_vault, monkeypatch) -> None:
    """sabnyx promote output must relativize the atlas dest against DATA_ROOT.

    Before the fix, result.relative_to(ROOT) and dest.relative_to(ROOT)
    raised ValueError when ROOT != DATA_ROOT.
    """
    from sabnyx.commands import promote as promote_mod

    p = _make_intake(tmp_vault["INTAKE"], "prom-test", _good_meta("bookmark"))
    monkeypatch.setattr(promote_mod, "INTAKE", tmp_vault["INTAKE"])
    monkeypatch.setattr(promote_mod, "DATA_ROOT", tmp_vault["DATA_ROOT"])
    monkeypatch.setattr(promote_mod, "ensure_tree", lambda: None)

    result = CliRunner().invoke(promote_mod.promote_cmd, [str(p), "--as", "reference"])
    assert result.exit_code == 0, result.output
    # Output should show vault/atlas/references/prom-test.md, not absolute
    assert "vault/atlas/references/" in result.output


# ---------------------------------------------------------------------------
# triage: intake list + pending.md subject + write display use DATA_ROOT
# ---------------------------------------------------------------------------


def test_triage_output_uses_data_root(tmp_vault, monkeypatch) -> None:
    """triage must relativize all paths against DATA_ROOT.

    Covers: lines 81, 87, 98, 101 (intake/obs display), line 112 (ledger
    subject), line 118 (write display).
    """
    from sabnyx.commands import triage as triage_mod

    monkeypatch.setattr(triage_mod, "INTAKE", tmp_vault["INTAKE"])
    monkeypatch.setattr(triage_mod, "OBSERVATIONS", tmp_vault["OBSERVATIONS"])
    monkeypatch.setattr(triage_mod, "REVIEW", tmp_vault["VAULT"] / "_review")
    monkeypatch.setattr(triage_mod, "DATA_ROOT", tmp_vault["DATA_ROOT"])
    monkeypatch.setattr(triage_mod, "ensure_tree", lambda: None)
    (tmp_vault["VAULT"] / "_review").mkdir(parents=True, exist_ok=True)

    # plant one intake file so the list branch runs
    _make_intake(tmp_vault["INTAKE"], "triage-item", _good_meta("thought"))

    result = CliRunner().invoke(triage_mod.triage_cmd, [])
    assert result.exit_code == 0, result.output
    # The write-display line mentions vault/_review/pending.md
    assert "vault/_review/pending.md" in result.output
    # pending.md content shows vault-relative paths
    pending = tmp_vault["VAULT"] / "_review" / "pending.md"
    content = pending.read_text()
    assert "vault/intake/triage-item.md" in content
    # ledger subject is relative too
    events = list(ledger.replay())
    subj = next(e.subject for e in events if e.kind == "triage.proposed")
    assert subj.startswith("vault/_review/")
    assert not Path(subj).is_absolute()


# ---------------------------------------------------------------------------
# health: broken-wikilink display + orphan display + schema-drift display
# ---------------------------------------------------------------------------


def test_health_displays_use_data_root(tmp_vault, monkeypatch) -> None:
    """health must relativize vault file paths against DATA_ROOT.

    Covers lines 142, 150, 157, 164. We construct a vault state that
    triggers BOTH the broken-wikilink and schema-drift display branches.
    """
    from sabnyx.commands import health as health_mod

    # Wire up the path constants health references directly. The vault slug
    # index now comes from context.vault_slug_index(), which resolves
    # paths.VAULT at call time — already redirected by the tmp_vault fixture —
    # so health no longer carries its own VAULT attribute to patch.
    monkeypatch.setattr(health_mod, "ATLAS", tmp_vault["ATLAS"])
    monkeypatch.setattr(health_mod, "INTAKE", tmp_vault["INTAKE"])
    monkeypatch.setattr(health_mod, "OBSERVATIONS", tmp_vault["OBSERVATIONS"])
    monkeypatch.setattr(health_mod, "DATA_ROOT", tmp_vault["DATA_ROOT"])

    # Create an atlas file that references a non-existent slug → broken wikilink
    concepts_dir = tmp_vault["ATLAS"] / "concepts"
    concepts_dir.mkdir(parents=True, exist_ok=True)
    broken_meta = {
        "type": "concept",
        "description": "a concept description long enough to pass validation checks",
        "tags": [],
        "status": "final",
        "promoted_from": "intake/src.md",
        "promoted_date": "2026-01-01",
        "last_verified": "2026-01-01",
        "proof": None,
    }
    fm.write(
        concepts_dir / "has-broken-link.md",
        broken_meta,
        "# Has Broken\n\n[[nonexistent-slug]]\n",
    )

    # Create a schema-drift file (raw YAML, no frontmatter delimiter → FrontmatterError)
    (tmp_vault["VAULT"] / "intake").mkdir(parents=True, exist_ok=True)
    bad_path = tmp_vault["INTAKE"] / "bad-schema.md"
    bad_path.write_text("no frontmatter here at all\n")

    result = CliRunner().invoke(health_mod.health_cmd, [])
    # health exits 1 when schema_drift is non-empty
    assert result.exit_code in (0, 1), result.output
    # broken wikilinks section must show vault-relative path
    assert "vault/atlas/concepts/has-broken-link.md" in result.output
    # No absolute paths from tmp_path in the output
    assert str(tmp_vault["DATA_ROOT"]) not in result.output
