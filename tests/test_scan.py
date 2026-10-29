"""Tests for the contradiction scan: dedup, approval gate, gates in health."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from click.testing import CliRunner

from sabnyx import fm, ledger
from sabnyx.schemas import ContradictionScan, ProposedObservation


def _atlas(
    dir_, slug, description, *, type_="concept", last_verified="2026-06-12", promoted=True, body=""
):
    meta = {
        "type": type_,
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


def test_scan_observation_records_producer_scan(tmp_vault, monkeypatch):
    from sabnyx import observations

    c = tmp_vault["ATLAS_CONCEPTS"]
    _atlas(c, "alpha", "a concept about alpha, long enough to satisfy the schema")
    _atlas(c, "beta", "a concept about beta, also long enough to satisfy the schema")

    result, _ = _run_scan(monkeypatch, ["--yes"])
    assert result.exit_code == 0, result.output
    (o,) = observations.observations("all")
    assert o.fm.producer == "scan"
    (ev,) = [e for e in ledger.replay() if e.kind == "observation.created"]
    assert ev.subject == o.rel
    assert ev.payload["producer"] == "scan"
    # both involves name real pages and the actor is trusted: identity-bearing
    assert ev.payload["obs_id"].startswith("sha256:")


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
    events_before = ledger.count()
    result, _ = _run_scan(monkeypatch, ["--dry-run"])
    assert result.exit_code == 0, result.output
    assert not list(tmp_vault["OBSERVATIONS"].glob("*-contradiction-*.md"))
    # ledger-write-free: no scan.completed on a dry run (the LLM is faked here;
    # in real runs only the honest llm.* call events would appear).
    assert "scan.completed" not in [e.kind for e in ledger.replay()]
    assert ledger.count() == events_before


def test_scan_skips_tiny_atlas(tmp_vault, monkeypatch):
    _atlas(tmp_vault["ATLAS_CONCEPTS"], "lonely", "only one file, nothing to compare it against")
    result, calls = _run_scan(monkeypatch, ["--yes"])
    assert result.exit_code == 0
    assert calls == []  # no LLM call when < 2 files
    assert "nothing to scan" in result.output


# -- recurrence through the shared writer (6a: M3, M4, D5b) ---------------------------

_DESC = "a concept page long enough to satisfy the atlas description schema"


def _four_pages(tmp_vault):
    for slug in ("alpha", "beta", "gamma", "delta"):
        _atlas(tmp_vault["ATLAS_CONCEPTS"], slug, f"{slug}: {_DESC}")


def _finding(a: str, b: str, title: str | None = None) -> ProposedObservation:
    return ProposedObservation(
        category="contradiction",
        title=title or f"{a} vs {b}",
        body=f"{a} says X; {b} says not-X.",
        involves=[a, b],
    )


def _scan_of(*findings: ProposedObservation) -> ContradictionScan:
    return ContradictionScan(summary="findings", findings=list(findings))


def _seed_contradiction(tmp_vault, name, involves, *, source_agent="sabnyx-scan", kind=None):
    meta = {
        "type": "observation",
        "category": "contradiction",
        "observed": "2026-06-12",
        "source_agent": source_agent,
        "status": "pending",
        "involves": involves,
    }
    if kind is not None:
        meta |= {
            "status": "promoted" if kind == "done" else "archived",
            "resolved": "2026-06-13",
            "resolution": f"closed as {kind}",
            "resolution_kind": kind,
        }
    p = tmp_vault["OBSERVATIONS"] / name
    fm.write(p, meta, f"# {name}\n\nseeded\n")
    return p


def _obs_files(tmp_vault) -> list[str]:
    return sorted(p.name for p in tmp_vault["OBSERVATIONS"].glob("*.md"))


def _kinds(kind: str) -> list:
    return [e for e in ledger.replay() if e.kind == kind]


def test_scan_second_run_on_unchanged_vault_files_nothing(tmp_vault, monkeypatch):
    # M3: the pair set is gone; the recurrence policy is what stops a re-file.
    _four_pages(tmp_vault)
    scan = _scan_of(_finding("alpha.md", "beta.md"), _finding("gamma", "delta"))
    first, _ = _run_scan(monkeypatch, ["--yes"], scan=scan)
    assert first.exit_code == 0, first.output
    files, created = _obs_files(tmp_vault), len(_kinds("observation.created"))
    assert len(files) == 2
    assert created == 2

    second, _ = _run_scan(monkeypatch, ["--yes"], scan=scan)
    assert second.exit_code == 0, second.output
    assert _obs_files(tmp_vault) == files  # 0 files
    assert len(_kinds("observation.created")) == created  # 0 observation.created
    assert "2 finding(s) already tracked" in second.output
    assert "no new contradictions" in second.output


def test_scan_second_run_on_a_later_day_files_nothing(tmp_vault, monkeypatch):
    # A new date prefix means no filename collision: only identity stops it.
    from datetime import date

    _four_pages(tmp_vault)
    scan = _scan_of(_finding("alpha", "beta"))
    monkeypatch.setattr(ledger, "today", lambda: date(2026, 6, 12))
    assert _run_scan(monkeypatch, ["--yes"], scan=scan)[0].exit_code == 0
    files = _obs_files(tmp_vault)
    monkeypatch.setattr(ledger, "today", lambda: date(2026, 6, 13))
    second, _ = _run_scan(monkeypatch, ["--yes"], scan=scan)
    assert second.exit_code == 0, second.output
    assert _obs_files(tmp_vault) == files
    assert len(_kinds("observation.created")) == 1


def test_scan_pair_in_path_md_and_bare_forms_is_one_signal(tmp_vault, monkeypatch):
    # D5b + identity: one pair named three ways in one LLM result files once,
    # and the count the operator confirms is the count filed.
    _four_pages(tmp_vault)
    scan = _scan_of(
        _finding("vault/atlas/concepts/alpha.md", "beta", title="first form"),
        _finding("alpha.md", "beta.md", title="second form"),
        _finding("Beta", "alpha", title="third form"),
    )
    result, _ = _run_scan(monkeypatch, [], scan=scan, input_text="y\n")
    assert result.exit_code == 0, result.output
    assert "file these 1 observation(s)?" in result.output
    assert "filed 1 contradiction observation(s)" in result.output
    assert len(_obs_files(tmp_vault)) == 1
    assert "2 finding(s) already tracked" in result.output


def test_scan_confirm_count_equals_real_filed_count(tmp_vault, monkeypatch):
    # dry -> confirm -> real: one result holding a fresh pair, the same pair
    # again, a pending pair, a dismissed pair, and two more fresh pairs.
    _four_pages(tmp_vault)
    _seed_contradiction(tmp_vault, "2026-06-12-contradiction-pend.md", ["gamma", "delta"])
    _seed_contradiction(
        tmp_vault, "2026-06-12-contradiction-gone.md", ["alpha", "delta"], kind="wontfix"
    )
    scan = _scan_of(
        _finding("alpha", "beta"),  # fresh
        _finding("beta.md", "alpha.md"),  # same pair again: deduped in-result
        _finding("gamma", "delta"),  # already pending
        _finding("alpha", "delta"),  # dismissed: suppressed
        _finding("alpha", "nowhere-page"),  # fresh: a Missing token still has a slug
        _finding("beta", "gamma"),  # fresh
    )
    result, _ = _run_scan(monkeypatch, [], scan=scan, input_text="y\n")
    assert result.exit_code == 0, result.output
    before = {"2026-06-12-contradiction-pend.md", "2026-06-12-contradiction-gone.md"}
    filed = set(_obs_files(tmp_vault)) - before
    assert "file these 3 observation(s)?" in result.output
    assert "filed 3 contradiction observation(s)" in result.output
    assert len(filed) == 3
    (done,) = _kinds("scan.completed")
    assert done.payload == {
        "files_scanned": 4,
        "findings": 6,
        "fresh": 3,
        "duplicates": 2,  # the in-result repeat + the pending pair
        "suppressed": 1,
        "invalid": 0,
    }
    p = done.payload  # the counts partition the findings
    assert p["fresh"] + p["duplicates"] + p["suppressed"] + p["invalid"] == p["findings"]
    assert "1 finding(s) dismissed earlier" in result.output


def test_scan_prose_twins_in_one_result_preview_as_one(tmp_vault, monkeypatch):
    # Prose (no id) dedupes by the file it would write: same title, same day.
    _four_pages(tmp_vault)
    scan = _scan_of(
        _finding("alpha", "alpha", title="self conflict"),  # same component twice: prose
        _finding("beta", "beta", title="self conflict"),  # same title -> same file
    )
    result, _ = _run_scan(monkeypatch, ["--yes"], scan=scan)
    assert result.exit_code == 0, result.output
    assert "filed 1 contradiction observation(s)" in result.output
    assert len(_obs_files(tmp_vault)) == 1
    assert "skip (exists)" not in result.output  # never offered, so never collided


def test_scan_pending_trusted_contradiction_blocks_refile(tmp_vault, monkeypatch):
    # A pending reweave finding run by tend (source_agent sabnyx-tend) is a
    # trusted signal: scan sees it as already tracked.
    _four_pages(tmp_vault)
    _seed_contradiction(
        tmp_vault,
        "2026-06-12-contradiction-tend.md",
        ["beta.md", "alpha"],
        source_agent="sabnyx-tend",
    )
    result, _ = _run_scan(monkeypatch, ["--yes"], scan=_scan_of(_finding("alpha", "beta")))
    assert result.exit_code == 0, result.output
    assert _obs_files(tmp_vault) == ["2026-06-12-contradiction-tend.md"]
    assert _kinds("observation.created") == []


def test_scan_untrusted_contradictions_block_nothing(tmp_vault, monkeypatch):
    # D14: a telegram-filed pending AND a telegram-filed dismissed contradiction
    # on the same pair have no identity, so they can neither block nor
    # suppress the trusted scan's finding.
    _four_pages(tmp_vault)
    _seed_contradiction(
        tmp_vault,
        "2026-06-12-contradiction-tg-open.md",
        ["alpha", "beta"],
        source_agent="sabnyx-telegram",
    )
    _seed_contradiction(
        tmp_vault,
        "2026-06-12-contradiction-tg-gone.md",
        ["alpha", "beta"],
        source_agent="sabnyx-telegram",
        kind="wontfix",
    )
    result, _ = _run_scan(monkeypatch, ["--yes"], scan=_scan_of(_finding("alpha", "beta")))
    assert result.exit_code == 0, result.output
    (ev,) = _kinds("observation.created")
    assert ev.payload["producer"] == "scan"
    assert _kinds("observation.suppressed") == []


def test_scan_dismissed_signal_is_counted_once(tmp_vault, monkeypatch):
    # M4 (ledger-asserting): a dismissed signal met by scan files nothing and
    # appends exactly one observation.suppressed with count 1.
    _four_pages(tmp_vault)
    head = _seed_contradiction(
        tmp_vault, "2026-06-12-contradiction-gone.md", ["alpha", "beta"], kind="wontfix"
    )
    result, _ = _run_scan(monkeypatch, ["--yes"], scan=_scan_of(_finding("beta", "alpha")))
    assert result.exit_code == 0, result.output
    assert _obs_files(tmp_vault) == [head.name]
    assert _kinds("observation.created") == []
    (ev,) = _kinds("observation.suppressed")
    assert ev.actor == "sabnyx-scan"
    assert ev.plane == "projection"
    assert ev.subject == f"vault/intake/.observations/{head.name}"
    assert ev.payload["count"] == 1
    assert ev.payload["kind"] == "wontfix"
    assert "skip (suppressed: wontfix)" in result.output


def test_scan_counts_suppressions_even_when_the_operator_declines(tmp_vault, monkeypatch):
    # The real pass runs the suppressed findings regardless of consent: they
    # write nothing, and the run did meet the dismissed signal.
    _four_pages(tmp_vault)
    _seed_contradiction(
        tmp_vault, "2026-06-12-contradiction-gone.md", ["alpha", "beta"], kind="obsolete"
    )
    scan = _scan_of(_finding("alpha", "beta"), _finding("gamma", "delta"))
    result, _ = _run_scan(monkeypatch, [], scan=scan, input_text="n\n")
    assert result.exit_code == 0, result.output
    assert "declined; nothing written" in result.output
    assert _kinds("observation.created") == []
    (ev,) = _kinds("observation.suppressed")
    assert ev.payload["kind"] == "obsolete"


def test_scan_dry_run_counts_and_ledgers_nothing(tmp_vault, monkeypatch):
    _four_pages(tmp_vault)
    _seed_contradiction(
        tmp_vault, "2026-06-12-contradiction-gone.md", ["alpha", "beta"], kind="wontfix"
    )
    files, events = _obs_files(tmp_vault), ledger.count()
    scan = _scan_of(_finding("alpha", "beta"), _finding("gamma", "delta"))
    result, _ = _run_scan(monkeypatch, ["--dry-run"], scan=scan)
    assert result.exit_code == 0, result.output
    assert "dry run: nothing written" in result.output
    assert _obs_files(tmp_vault) == files
    assert ledger.count() == events


def test_scan_reopens_a_done_signal(tmp_vault, monkeypatch):
    _four_pages(tmp_vault)
    head = _seed_contradiction(
        tmp_vault, "2026-06-12-contradiction-fixed.md", ["alpha", "beta"], kind="done"
    )
    result, _ = _run_scan(monkeypatch, ["--yes"], scan=_scan_of(_finding("alpha", "beta")))
    assert result.exit_code == 0, result.output
    assert "filed 1 contradiction observation(s)" in result.output
    (ev,) = _kinds("observation.created")
    assert ev.payload["supersedes"] == f"vault/intake/.observations/{head.name}"


# -- health gates -----------------------------------------------------------------------


def test_health_currency_and_materia_gates(tmp_vault):
    from sabnyx.commands import health as health_mod

    c = tmp_vault["ATLAS_CONCEPTS"]
    r = tmp_vault["ATLAS_REFERENCES"]
    old = (datetime.now(UTC) - timedelta(days=400)).date().isoformat()
    # Gate 5 applies to time-sensitive types (references) only.
    _atlas(r, "fresh", "a recently verified reference, long enough for schema", type_="reference")
    _atlas(
        r,
        "stale",
        "a long-unverified reference, long enough for the schema",
        type_="reference",
        last_verified=old,
    )
    # A never-verified concept is NOT stale: concepts don't go stale on a clock.
    _atlas(c, "timeless", "a concept never verified, long enough for schema", last_verified=None)
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
    assert audit.payload["atlas"] == 5
    assert audit.payload["stale_atlas"] == 1  # only "stale" (Gate 5 is reference-only)
    assert audit.payload["untracked_provenance"] == 1  # only "untracked"
    # cost_ratio is a fixed-precision string (hashed payloads forbid floats).
    assert audit.payload["cost_ratio"] == "0.000"  # no intake files
