"""Tests for the contradiction scan: dedup, approval gate, gates in health."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from click.testing import CliRunner

from sabnyx import fm, ledger
from sabnyx.schemas import ContradictionScan, ProposedObservation


def _atlas(dir_, slug, description, *, last_verified="2026-06-12", promoted=True, body=""):
    meta = {
        "type": "concept",
        "description": description,
        "tags": [],
        "status": "final",
        "promoted_from": "intake/x.md" if promoted else None,
        "promoted_date": "2026-06-12",
        "last_verified": last_verified,
        "proof": None,
    }
    p = dir_ / f"{slug}.md"
    fm.write(p, meta, f"# {slug}\n\n{body}")
    return p


# -- scan -------------------------------------------------------------------------------


SCAN = ContradictionScan(
    summary="one conflict found",
    findings=[
        ProposedObservation(
            category="contradiction",
            title="alpha vs beta on X",
            body="alpha says X; beta says not-X. Verify alpha's source.",
            involves=["alpha.md", "beta.md"],
        )
    ],
)


def _run_scan(monkeypatch, args, scan=SCAN, input_text=None):
    from sabnyx.commands import scan as scan_mod

    calls: list[dict] = []

    def fake(**kwargs):
        calls.append(kwargs)
        return scan

    monkeypatch.setattr(scan_mod.llm, "parse_structured", fake)
    runner = CliRunner()
    return runner.invoke(scan_mod.scan_cmd, args, input=input_text), calls


def test_scan_writes_contradiction(tmp_vault, monkeypatch):
    c = tmp_vault["ATLAS_CONCEPTS"]
    _atlas(c, "alpha", "a concept about alpha, long enough to satisfy the schema")
    _atlas(c, "beta", "a concept about beta, also long enough to satisfy the schema")

    result, calls = _run_scan(monkeypatch, ["--yes"])
    assert result.exit_code == 0, result.output
    assert calls[0]["role"] == "deep"
    obs = list(tmp_vault["OBSERVATIONS"].glob("*-contradiction-*.md"))
    assert len(obs) == 1
    meta, _, _ = fm.load(obs[0])
    assert meta["involves"] == ["alpha.md", "beta.md"]
    kinds = [e.kind for e in ledger.replay()]
    assert "scan.completed" in kinds


def test_scan_dedupes_against_pending(tmp_vault, monkeypatch):
    c = tmp_vault["ATLAS_CONCEPTS"]
    _atlas(c, "alpha", "a concept about alpha, long enough to satisfy the schema")
    _atlas(c, "beta", "a concept about beta, also long enough to satisfy the schema")
    # pre-existing pending contradiction over the same pair
    obs = tmp_vault["OBSERVATIONS"] / "2026-06-12-contradiction-existing.md"
    fm.write(
        obs,
        {
            "type": "observation",
            "category": "contradiction",
            "observed": "2026-06-12",
            "source_agent": "manual",
            "status": "pending",
            "involves": ["beta.md", "alpha.md"],  # order-independent
        },
        "# existing\n\nalready tracked\n",
    )

    result, _ = _run_scan(monkeypatch, ["--yes"])
    assert result.exit_code == 0, result.output
    assert "already tracked" in result.output
    # still only the pre-existing one; no duplicate filed
    assert len(list(tmp_vault["OBSERVATIONS"].glob("*-contradiction-*.md"))) == 1


def test_scan_dry_run_writes_nothing(tmp_vault, monkeypatch):
    c = tmp_vault["ATLAS_CONCEPTS"]
    _atlas(c, "alpha", "a concept about alpha, long enough to satisfy the schema")
    _atlas(c, "beta", "a concept about beta, also long enough to satisfy the schema")
    result, _ = _run_scan(monkeypatch, ["--dry-run"])
    assert result.exit_code == 0, result.output
    assert not list(tmp_vault["OBSERVATIONS"].glob("*-contradiction-*.md"))


def test_scan_skips_tiny_atlas(tmp_vault, monkeypatch):
    _atlas(tmp_vault["ATLAS_CONCEPTS"], "lonely", "only one file, nothing to compare it against")
    result, calls = _run_scan(monkeypatch, ["--yes"])
    assert result.exit_code == 0
    assert calls == []  # no LLM call when < 2 files
    assert "nothing to scan" in result.output


# -- health gates -----------------------------------------------------------------------


def test_health_currency_and_materia_gates(tmp_vault):
    from sabnyx.commands import health as health_mod

    c = tmp_vault["ATLAS_CONCEPTS"]
    old = (datetime.now(UTC) - timedelta(days=400)).date().isoformat()
    _atlas(c, "fresh", "a recently verified concept, long enough for the schema")
    _atlas(c, "stale", "a long-unverified concept, long enough for the schema", last_verified=old)
    # provenance via source comment instead of promoted_from
    _atlas(
        c,
        "sourced",
        "born in atlas but cites a source, long enough for the schema",
        promoted=False,
        body="<!-- source-url: https://example.com -->\n",
    )
    # no provenance at all
    _atlas(c, "untracked", "born in atlas with no provenance at all, long enough", promoted=False)

    runner = CliRunner()
    result = runner.invoke(health_mod.health_cmd, [])
    assert result.exit_code == 0, result.output

    audit = [e for e in ledger.replay() if e.kind == "health.audited"][-1]
    assert audit.payload["atlas"] == 4
    assert audit.payload["stale_atlas"] == 1  # only "stale"
    assert audit.payload["untracked_provenance"] == 1  # only "untracked"
    # cost_ratio is a fixed-precision string (hashed payloads forbid floats).
    assert audit.payload["cost_ratio"] == "0.000"  # no intake files
