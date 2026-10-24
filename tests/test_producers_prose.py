"""6b: reweave, ingest and `new` through the shared writer (IDD §6, D2, D4, D13).

M4 for the prose producers: a dismissed signal met by one entry-point call
(one reweave_one, one ingest apply) is counted once in the ledger and files
nothing. D14: a telegram-actor contradiction is prose and blocks nothing.
The prompt-text tests prove the only change to each prompt is the one
sentence about exactly two `involves`.
"""

from __future__ import annotations

import hashlib
from datetime import date

import pytest
from click.testing import CliRunner
from rich.console import Console

from sabnyx import fm, ledger, paths, services, writers
from sabnyx.commands import reweave as rw
from sabnyx.schemas import IngestChangeset, ProposedObservation, ReweaveChangeset

_DESC = "a concept page long enough to satisfy the atlas description schema"
_QUIET = Console(quiet=True)


def _page(slug: str) -> None:
    fm.write(
        paths.ATLAS / "concepts" / f"{slug}.md",
        {"type": "concept", "description": f"{slug}: {_DESC}"},
        f"# {slug}\n",
    )


def _seed(name: str, involves: list[str], *, source_agent="sabnyx-scan", kind=None) -> str:
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
    fm.write(paths.OBSERVATIONS / name, meta, f"# {name}\n")
    return f"vault/intake/.observations/{name}"


def _contradiction(a="alpha", b="beta", title="alpha vs beta") -> ProposedObservation:
    return ProposedObservation(
        category="contradiction", title=title, body="one says X, the other not-X", involves=[a, b]
    )


def _kinds(kind: str) -> list:
    return [e for e in ledger.replay() if e.kind == kind]


def _files() -> list[str]:
    return sorted(p.name for p in paths.OBSERVATIONS.glob("*.md"))


@pytest.fixture
def pages(tmp_vault):
    for slug in ("alpha", "beta", "gamma"):
        _page(slug)
    paths.OBSERVATIONS.mkdir(parents=True, exist_ok=True)
    return tmp_vault


# -- ingest: one apply_ingest call is one run -------------------------------------


def _apply(*observations: ProposedObservation, actor: str = services.ACTOR_INGEST) -> list[str]:
    proposal = services.IngestProposal(
        changeset=IngestChangeset(summary="s", observations=list(observations)),
        depth="shallow",
        role="fast",
    )
    return services.apply_ingest(
        proposal,
        title="a source",
        source_kind="manual",
        source_url=None,
        src_rel="sources/manual/a-source.md",
        console=_QUIET,
        actor=actor,
    )


def test_ingest_dismissed_signal_is_counted_once_per_apply(pages):
    head = _seed("2026-06-12-contradiction-gone.md", ["alpha", "beta"], kind="wontfix")
    before = _files()
    written = _apply(_contradiction(), _contradiction("beta.md", "alpha.md", title="again"))
    assert written == []
    assert _files() == before
    assert _kinds("observation.created") == []
    (ev,) = _kinds("observation.suppressed")  # two hits, one run, one event
    assert ev.actor == services.ACTOR_INGEST
    assert ev.subject == head
    assert ev.payload["count"] == 2
    assert ev.payload["kind"] == "wontfix"
    # ordering: counted after the run's own ingest.applied
    kinds = [e.kind for e in ledger.replay()]
    assert kinds.index("ingest.applied") < kinds.index("observation.suppressed")


def test_ingest_two_applies_are_two_runs(pages):
    _seed("2026-06-12-contradiction-gone.md", ["alpha", "beta"], kind="obsolete")
    _apply(_contradiction())
    _apply(_contradiction())
    assert [e.payload["count"] for e in _kinds("observation.suppressed")] == [1, 1]


def test_ingest_no_suppression_ledgers_nothing_extra(pages):
    _apply(_contradiction())
    assert _kinds("observation.suppressed") == []
    (ev,) = _kinds("observation.created")
    assert ev.payload["producer"] == "ingest"


def test_ingest_telegram_contradiction_is_prose_and_blocks_nothing(pages):
    # D14: identity is gated on the actor, and a telegram ingest's actor is
    # untrusted. Its contradiction is written (prose, no obs_id) and then
    # neither blocks nor suppresses a trusted ingest of the same pair.
    _apply(_contradiction(title="from the phone"), actor="sabnyx-telegram")
    (tg,) = _kinds("observation.created")
    assert "obs_id" not in tg.payload
    _seed(
        "2026-06-12-contradiction-tg-gone.md",
        ["alpha", "beta"],
        source_agent="sabnyx-telegram",
        kind="wontfix",
    )

    _apply(_contradiction(title="from the desk"))
    created = _kinds("observation.created")
    assert len(created) == 2
    assert created[1].payload["obs_id"].startswith("sha256:")
    assert _kinds("observation.suppressed") == []


# -- reweave: one reweave_one call is one run --------------------------------------


def _reweave(monkeypatch, *observations: ProposedObservation, actor="sabnyx-reweave"):
    changeset = ReweaveChangeset(summary="s", related_additions=[], observations=list(observations))
    monkeypatch.setattr(rw.llm, "parse_structured", lambda **_kw: changeset)
    from sabnyx import context

    target = paths.ATLAS / "concepts" / "alpha.md"
    return rw.reweave_one(
        target,
        context.slug_index(paths.ATLAS),
        role="standard",
        actor=actor,
        yes=True,
        dry_run=False,
        console=_QUIET,
    )


def test_reweave_dismissed_signal_is_counted_once_per_call(pages, monkeypatch):
    head = _seed("2026-06-12-contradiction-gone.md", ["alpha", "gamma"], kind="duplicate")
    before = _files()
    linked, written = _reweave(monkeypatch, _contradiction("alpha", "gamma"))
    assert written == []
    assert _files() == before
    assert _kinds("observation.created") == []
    (ev,) = _kinds("observation.suppressed")
    assert ev.actor == "sabnyx-reweave"
    assert ev.subject == head
    assert ev.payload == {"obs_id": ev.payload["obs_id"], "count": 1, "kind": "duplicate"}
    kinds = [e.kind for e in ledger.replay()]
    assert kinds.index("reweave.completed") < kinds.index("observation.suppressed")


def test_reweave_run_by_tend_counts_under_the_tend_actor(pages, monkeypatch):
    # producer is the code path (reweave); the actor is whoever ran it.
    _seed("2026-06-12-contradiction-gone.md", ["alpha", "gamma"], kind="wontfix")
    _reweave(monkeypatch, _contradiction("alpha", "gamma"), actor="sabnyx-tend")
    (ev,) = _kinds("observation.suppressed")
    assert ev.actor == "sabnyx-tend"


def test_reweave_pending_telegram_contradiction_blocks_nothing(pages, monkeypatch):
    _seed("2026-06-12-contradiction-tg.md", ["alpha", "gamma"], source_agent="sabnyx-telegram")
    _, written = _reweave(monkeypatch, _contradiction("alpha", "gamma"))
    assert len(written) == 1
    (ev,) = _kinds("observation.created")
    assert ev.payload["producer"] == "reweave"
    assert ev.payload["obs_id"].startswith("sha256:")


# -- prompts: the one sentence about exactly two involves, nothing else -----------

# sha256 of each prompt at the commit before 6b. Reversing the one sentence
# must reproduce it exactly, so no other byte of the prompt moved.
_REWEAVE_BEFORE = "3db92b6c1d9047386f34acd8a41885c7e90a6d3cee2ae8a983098df3eb6db320"
_INGEST_BEFORE = "6fd6f847cc4cf381fc2f9cf16b1d72515fee061aa46d0a6b9714bb81ab1c58eb"

_REWEAVE_OLD = (
    "  observation with category: contradiction, BOTH files in `involves`, and a\n"
    "  suggested resolution in the body. This is how the vault detects stale claims."
)
_REWEAVE_NEW = (
    "  observation with category: contradiction, exactly two files in `involves`\n"
    "  (the target and the conflicting file), and a suggested resolution in the\n"
    "  body. This is how the vault detects stale claims."
)
_INGEST_OLD = "  propose an observation (category: contradiction) naming the file."
_INGEST_NEW = (
    "  propose an observation (category: contradiction) with exactly two files in\n"
    "  `involves`: the existing file and the intake file you propose for the source."
)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@pytest.mark.parametrize(
    ("prompt", "old", "new", "before"),
    [
        (rw._TASK_SYSTEM, _REWEAVE_OLD, _REWEAVE_NEW, _REWEAVE_BEFORE),
        (services.INGEST_TASK_SYSTEM, _INGEST_OLD, _INGEST_NEW, _INGEST_BEFORE),
    ],
    ids=["reweave", "ingest"],
)
def test_prompt_change_is_the_one_sentence(prompt, old, new, before):
    assert prompt.count(new) == 1
    assert "exactly two" in new
    assert _sha(prompt.replace(new, old)) == before


# -- new --type observation through the shared writer ----------------------------


def _new(*args):
    from sabnyx.commands import new as new_mod

    return CliRunner().invoke(new_mod.new_cmd, list(args))


_OBS_ARGS = [
    "--type",
    "observation",
    "--category",
    "friction",
    "--title",
    "Triage Is Slow",
    "--description",
    "triage takes long on big queues",
]


def test_new_observation_goes_through_the_shared_writer(tmp_vault, monkeypatch):
    monkeypatch.setattr(ledger, "today", lambda: date(2026, 6, 12))  # the UTC clock seam
    result = _new(*_OBS_ARGS)
    assert result.exit_code == 0, result.output
    path = paths.OBSERVATIONS / "2026-06-12-friction-triage-is-slow.md"
    assert result.output.strip() == str(path)  # the path, and nothing from the writer
    meta, body, _ = fm.load(path)
    assert meta["producer"] == "manual"
    assert meta["source_agent"] == "sabnyx-cli"
    assert "triage takes long on big queues" in body
    (ev,) = _kinds("observation.created")
    assert ev.actor == "sabnyx-cli"
    assert ev.payload["producer"] == "manual"
    log = paths.KNOWLEDGE_LOG.read_text(encoding="utf-8")
    assert "new | Triage Is Slow" in log


def test_new_observation_name_taken_exits_1(tmp_vault):
    assert _new(*_OBS_ARGS).exit_code == 0
    second = _new(*_OBS_ARGS)
    assert second.exit_code == 1
    assert "refusing to overwrite" in second.output
    assert len(_kinds("observation.created")) == 1


def test_new_observation_invalid_exits_3(tmp_vault, monkeypatch):
    # Every CLI-reachable observation is valid (category is a choice, no
    # target), so probe the mapping at the writer seam.
    from sabnyx.commands import new as new_mod

    monkeypatch.setattr(
        new_mod.writers,
        "write_observation",
        lambda *_a, **_k: writers.WriteOutcome("invalid", None, error="target: bad"),
    )
    result = _new(*_OBS_ARGS)
    assert result.exit_code == 3
    assert "invalid frontmatter, nothing written: target: bad" in result.output


@pytest.mark.parametrize("kind", ["already_pending", "suppressed"])
def test_new_observation_tracked_signal_exits_1_not_silently_0(tmp_vault, monkeypatch, kind):
    from sabnyx.commands import new as new_mod

    p = paths.OBSERVATIONS / "2026-06-12-friction-x.md"
    extra = {"resolution_kind": "wontfix"} if kind == "suppressed" else {}
    monkeypatch.setattr(
        new_mod.writers,
        "write_observation",
        lambda *_a, **_k: writers.WriteOutcome(kind, p, obs_id="sha256:" + "0" * 64, **extra),
    )
    result = _new(*_OBS_ARGS)
    assert result.exit_code == 1
    assert f"already tracked ({kind})" in result.output


def test_new_observation_without_category_still_exits_2(tmp_vault):
    result = _new("--type", "observation", "--title", "x", "--description", "y")
    assert result.exit_code == 2
