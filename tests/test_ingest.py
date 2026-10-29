"""Tests for the LLM boundary, sources layer, and ingest command.

No network: the LLM call site is monkeypatched with a canned changeset,
and all vault/ops paths are redirected to tmp_path.
"""

from __future__ import annotations

import pytest
from click.testing import CliRunner

from sabnyx import ledger, llm, sources
from sabnyx.schemas import IngestChangeset, ProposedIntake, ProposedObservation

# -- router / roles ---------------------------------------------------------------


def test_role_for_message_overrides():
    assert llm.role_for_message("[fast] summarize this") == "fast"
    assert llm.role_for_message("please [deep] analysis") == "deep"


def test_role_for_message_scoring():
    assert llm.role_for_message("what time is it") == "fast"
    long_reasoning = (
        "why does the architecture of this system create a trade-off between "
        "consistency and availability? compare the approaches and synthesize "
        "a recommendation. " * 6
    )
    assert llm.role_for_message(long_reasoning) == "deep"


def test_role_for_depth():
    assert llm.role_for_depth("shallow") == "fast"
    assert llm.role_for_depth("standard") == "standard"
    assert llm.role_for_depth("standard-plus") == "standard"
    assert llm.role_for_depth("deep") == "deep"


def test_model_for_env_override(monkeypatch):
    monkeypatch.setenv("SABNYX_MODEL_DEEP", "my-custom-model")
    assert llm.model_for("deep") == "my-custom-model"


def test_provider_selection(monkeypatch):
    assert llm.provider() == "anthropic"
    monkeypatch.setenv("SABNYX_LLM_PROVIDER", "openrouter")
    assert llm.provider() == "openrouter"
    monkeypatch.setenv("SABNYX_LLM_PROVIDER", "nonsense")
    with pytest.raises(ValueError, match="unknown SABNYX_LLM_PROVIDER"):
        llm.provider()


def test_extract_json_variants():
    assert llm._extract_json('{"a": 1}') == '{"a": 1}'
    assert llm._extract_json('```json\n{"a": 1}\n```') == '{"a": 1}'
    assert llm._extract_json('Sure! Here it is: {"a": 1} hope that helps') == '{"a": 1}'


# -- sources layer -----------------------------------------------------------------


def test_classify_depth():
    assert sources.classify_depth("short note") == "shallow"
    assert sources.classify_depth("word " * 1000) == "standard"
    assert sources.classify_depth("word " * 3000) == "standard-plus"
    assert sources.classify_depth("word " * 6000) == "deep"
    assert sources.classify_depth("abstract", source_url="https://arxiv.org/abs/1") == "deep"


def test_capture_is_write_once_for_identical_content(tmp_vault):
    p = sources.capture(text="hello world", slug="My Source", actor="pytest")
    assert p.name == "my-source.md"
    assert p.read_text() == "hello world"
    with pytest.raises(FileExistsError, match="already captured"):
        sources.capture(text="hello world", slug="My Source", actor="pytest")
    kinds = [e.kind for e in ledger.replay()]
    assert kinds == ["source.captured"]


def test_capture_slug_collision_with_different_content_disambiguates(tmp_vault):
    # Two different messages sharing a title-derived slug (first-8-words titles
    # collide easily) must both land — the second under a content-hash suffix.
    p1 = sources.capture(text="the same opening phrase then A", slug="The Same", actor="pytest")
    p2 = sources.capture(text="the same opening phrase then B", slug="The Same", actor="pytest")
    assert p1 != p2
    assert p2.name.startswith("the-same-")
    assert p2.read_text() == "the same opening phrase then B"
    # write-once still holds per content: recapturing B is refused
    with pytest.raises(FileExistsError, match="already captured"):
        sources.capture(text="the same opening phrase then B", slug="The Same", actor="pytest")


def test_capture_reuses_identical_content_when_requested(tmp_vault):
    first = sources.capture(text="same", slug="Same", actor="pytest")
    reused = sources.capture(text="same", slug="Same", actor="pytest", reuse_existing=True)
    assert reused == first
    assert [e.kind for e in ledger.replay()] == ["source.captured"]


def test_capture_preserves_distinct_provenance_variants(tmp_vault):
    paths = [
        sources.capture(
            text="same text",
            slug="same title",
            actor=f"actor-{n}",
            source_url=f"https://example.com/{n}",
        )
        for n in range(3)
    ]
    assert len(set(paths)) == 3
    for n, path in enumerate(paths):
        assert f"source-url: https://example.com/{n}" in path.read_text()


def test_capture_records_url_header(tmp_vault):
    p = sources.capture(text="body", slug="x", actor="pytest", source_url="https://example.com/a")
    content = p.read_text()
    assert content.startswith("<!-- source-url: https://example.com/a -->")
    assert content.endswith("body")


# -- ingest end-to-end (faked LLM) ----------------------------------------------------


CANNED = IngestChangeset(
    summary="A test source about markdown-first publishing.",
    intake_files=[
        ProposedIntake(
            type="thought",
            title="Markdown First Publishing",
            description="Publishing markdown as the canonical surface, HTML as projection only.",
            tags=["publishing"],
            body="Agents read markdown natively; serve it raw.",
        )
    ],
    observations=[
        ProposedObservation(
            category="connection",
            title="relates to markdown is the new html",
            body="Overlaps an existing concept.",
            involves=["markdown-is-the-new-html.md"],
        )
    ],
)


def _run_ingest(tmp_vault, monkeypatch, args, input_text="some source text\n"):
    from sabnyx.commands import ingest as ingest_mod

    calls: list[dict] = []

    def fake_parse_structured(**kwargs):
        calls.append(kwargs)
        return CANNED

    monkeypatch.setattr(ingest_mod.llm, "parse_structured", fake_parse_structured)
    runner = CliRunner()
    result = runner.invoke(ingest_mod.ingest_cmd, args, input=input_text)
    return result, calls


def test_ingest_stdin_applies_changeset(tmp_vault, monkeypatch):
    result, calls = _run_ingest(
        tmp_vault,
        monkeypatch,
        ["--stdin", "--title", "Test Source", "--yes"],
        input_text="a tiny source about markdown\n",
    )
    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    assert calls[0]["output_format"] is IngestChangeset
    assert calls[0]["role"] == "fast"  # tiny source -> shallow -> fast

    # raw source captured, intake file + observation written and valid
    assert (tmp_vault["SOURCES"] / "test-source.md").exists()
    intake = tmp_vault["INTAKE"] / "markdown-first-publishing.md"
    assert intake.exists()
    text = intake.read_text()
    assert "type: thought" in text
    assert "source-id:sabnyx-sources:test-source.md" in text
    obs = list(tmp_vault["OBSERVATIONS"].glob("*-connection-*.md"))
    assert len(obs) == 1

    # ledger trail: capture -> proposal -> writes -> applied
    kinds = [e.kind for e in ledger.replay()]
    assert kinds == [
        "source.captured",
        "ingest.proposed",
        "intake.created",
        "observation.created",
        "ingest.applied",
    ]

    # gate 3: grep-able knowledge log line
    log = tmp_vault["KNOWLEDGE_LOG"].read_text()
    assert "] ingest | Test Source" in log


def test_ingest_observation_records_producer_ingest(tmp_vault, monkeypatch):
    from sabnyx import observations

    result, _ = _run_ingest(
        tmp_vault,
        monkeypatch,
        ["--stdin", "--title", "Test Source", "--yes"],
        input_text="a tiny source about markdown\n",
    )
    assert result.exit_code == 0, result.output
    (o,) = observations.observations("all")
    assert o.fm.producer == "ingest"
    assert o.fm.source_agent == "sabnyx-ingest"
    (ev,) = [e for e in ledger.replay() if e.kind == "observation.created"]
    assert ev.subject == o.rel
    assert ev.payload["producer"] == "ingest"


def test_ingest_dry_run_writes_nothing(tmp_vault, monkeypatch):
    result, calls = _run_ingest(tmp_vault, monkeypatch, ["--stdin", "--title", "Dry", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    assert not list(tmp_vault["SOURCES"].glob("*.md"))
    assert not list(tmp_vault["INTAKE"].glob("*.md"))


def test_ingest_declined_retains_and_reuses_capture(tmp_vault, monkeypatch, tmp_path):
    # Declining derived writes must not delete the immutable raw source. A later
    # ingest reuses it rather than being dead-ended by write-once capture.
    src = tmp_path / "declined-input.txt"
    src.write_text("source text\n")
    result, _calls = _run_ingest(
        tmp_vault,
        monkeypatch,
        [str(src), "--title", "Declined"],
        input_text="n\n",  # answer 'n' at the approval prompt
    )
    assert result.exit_code == 0, result.output
    assert (tmp_vault["SOURCES"] / "declined.md").exists()
    assert not list(tmp_vault["INTAKE"].glob("*.md"))
    kinds = [e.kind for e in ledger.replay()]
    assert "ingest.failed" in kinds  # the declined derivation is ledgered
    # and the same content can be re-ingested afterwards
    result2, _ = _run_ingest(tmp_vault, monkeypatch, [str(src), "--title", "Declined", "--yes"])
    assert result2.exit_code == 0, result2.output
    assert (tmp_vault["SOURCES"] / "declined.md").exists()


def test_ingest_extraction_failure_retains_and_reuses_capture(tmp_vault, monkeypatch):
    """An LLM failure retains the immutable artifact, ledgers the failed
    derivation, and a retry reuses the same source."""
    from rich.console import Console

    from sabnyx import services

    def boom(**kwargs):
        raise RuntimeError("provider exploded")

    monkeypatch.setattr(services.llm, "parse_structured", boom)
    with pytest.raises(RuntimeError, match="provider exploded"):
        services.ingest_text(
            text="some source text",
            title="Poisoned Slug",
            source_kind="manual",
            source_url=None,
            console=Console(),
        )
    assert (tmp_vault["SOURCES"] / "poisoned-slug.md").exists()
    events = list(ledger.replay())
    kinds = [e.kind for e in events]
    assert kinds == ["source.captured", "ingest.failed"]
    failed = events[-1]
    assert "provider exploded" in failed.payload["reason"]
    assert failed.payload["capture_retained"] is True

    # retry with a working model succeeds — the slug is not dead
    monkeypatch.setattr(services.llm, "parse_structured", lambda **kw: CANNED)
    summary, written = services.ingest_text(
        text="some source text",
        title="Poisoned Slug",
        source_kind="manual",
        source_url=None,
        console=Console(),
    )
    assert (tmp_vault["SOURCES"] / "poisoned-slug.md").exists()
    assert written


def test_ingest_cli_extraction_failure_retains_capture(tmp_vault, monkeypatch):
    from sabnyx.commands import ingest as ingest_mod

    def boom(**kwargs):
        raise RuntimeError("provider exploded")

    monkeypatch.setattr(ingest_mod.llm, "parse_structured", boom)
    result = CliRunner().invoke(
        ingest_mod.ingest_cmd,
        ["--stdin", "--title", "Doomed"],
        input="doomed source text\n",
    )
    assert result.exit_code == 1, result.output
    assert "retained" in result.output
    assert (tmp_vault["SOURCES"] / "doomed.md").exists()
    assert "ingest.failed" in [e.kind for e in ledger.replay()]


def test_ingest_model_reads_exactly_the_captured_text(tmp_vault, monkeypatch):
    """Gate 3: the immutable artifact must match the extraction input — an
    oversized source is truncated once, and the model sees that truncation."""
    from rich.console import Console

    from sabnyx import services

    monkeypatch.setattr(sources, "MAX_SOURCE_CHARS", 50)
    seen: list[str] = []

    def fake_parse_structured(**kwargs):
        seen.append(kwargs["user"])
        return CANNED

    monkeypatch.setattr(services.llm, "parse_structured", fake_parse_structured)
    oversized = "a" * 40 + "b" * 100  # truncates to 40 a's + 10 b's
    services.ingest_text(
        text=oversized,
        title="Big Source",
        source_kind="manual",
        source_url=None,
        console=Console(),
    )
    captured = (tmp_vault["SOURCES"] / "big-source.md").read_text()
    assert captured == oversized[:50]
    assert captured in seen[0]  # model got exactly the truncated text...
    assert "b" * 11 not in seen[0]  # ...never the untruncated overflow


def test_ingest_requires_exactly_one_input(tmp_vault, monkeypatch):
    from sabnyx.commands import ingest as ingest_mod

    runner = CliRunner()
    result = runner.invoke(ingest_mod.ingest_cmd, [])
    assert result.exit_code == 2


def test_ingest_downgrades_urlless_bookmark_to_note(tmp_vault, monkeypatch):
    """The model may classify pasted text as `bookmark` with no URL in sight;
    the writer downgrades it to `note` instead of writing an invalid file."""
    from sabnyx.commands import ingest as ingest_mod

    urlless_bookmark = IngestChangeset(
        summary="pasted text the model took for a bookmark",
        intake_files=[
            ProposedIntake(
                type="bookmark",
                title="Looks Like A Bookmark",
                description="a pasted snippet the model classified as a bookmark, no URL known",
                tags=[],
                body="No URL anywhere in this text.",
            )
        ],
    )
    monkeypatch.setattr(ingest_mod.llm, "parse_structured", lambda **kw: urlless_bookmark)
    result = CliRunner().invoke(
        ingest_mod.ingest_cmd,
        ["--stdin", "--title", "Pasted", "--yes"],
        input="pasted text with no url\n",
    )
    assert result.exit_code == 0, result.output
    assert "downgrade" in result.output
    intake = tmp_vault["INTAKE"] / "looks-like-a-bookmark.md"
    assert intake.exists()
    text = intake.read_text()
    assert "type: note" in text
    assert "type: bookmark" not in text


# -- provenance trust: remote content must not be auto-promotable ------------------


def test_local_ingest_is_trusted_provenance(tmp_vault, monkeypatch):
    from sabnyx import config, services

    result, _ = _run_ingest(tmp_vault, monkeypatch, ["--stdin", "--title", "Local Source", "--yes"])
    assert result.exit_code == 0, result.output
    intake = (tmp_vault["INTAKE"] / "markdown-first-publishing.md").read_text()
    assert "source_agent: sabnyx-ingest" in intake
    # and that agent is in the default trusted set (would auto-promote)
    assert services.ACTOR_INGEST in config.trusted_sources()


def test_url_ingest_is_non_trusted_provenance(tmp_vault, monkeypatch):
    from sabnyx import config, services
    from sabnyx.commands import ingest as ingest_mod

    monkeypatch.setattr(ingest_mod.sources, "fetch_url", lambda url: "remote body text\n")
    result, _ = _run_ingest(tmp_vault, monkeypatch, ["--url", "https://example.com/post", "--yes"])
    assert result.exit_code == 0, result.output

    intake = (tmp_vault["INTAKE"] / "markdown-first-publishing.md").read_text()
    assert "source_agent: sabnyx-ingest-web" in intake
    # the web actor is NOT trusted -> cannot auto-promote unattended
    assert services.ACTOR_INGEST_WEB not in config.trusted_sources()


def test_auto_promote_rejects_web_provenance():
    from sabnyx import services

    base = {
        "type": "bookmark",
        "description": "a sufficiently long description of a remote bookmark source",
        "source": "web",
        "status": "draft",
    }
    trusted = {services.ACTOR_INGEST}
    common = {"trusted": trusted, "atlas_slugs": set(), "source_slug": "x"}

    web = services.auto_promote_decision(
        {**base, "source_agent": services.ACTOR_INGEST_WEB}, "intake", **common
    )
    assert not web.promotable
    assert any("not in trusted" in r for r in web.reasons)

    local = services.auto_promote_decision(
        {**base, "source_agent": services.ACTOR_INGEST}, "intake", **common
    )
    assert local.promotable


def test_walk_vault_md_excludes_sources(tmp_vault):
    from sabnyx import paths as paths_mod
    from sabnyx import util

    (tmp_vault["SOURCES"] / "raw.md").write_text("no frontmatter here")
    (tmp_vault["INTAKE"] / "x.md").write_text("---\ntype: thought\n---\n")
    found = [p for p in tmp_vault["VAULT"].rglob("*.md") if paths_mod.SOURCES not in p.parents]
    # the util walk applies the same exclusion via paths.SOURCES
    walked = util.walk_vault_md(tmp_vault["VAULT"])
    assert all(paths_mod.SOURCES not in p.parents for p in walked)
    assert (tmp_vault["INTAKE"] / "x.md") in found
