"""A source that was already ingested is not extracted a second time.

Sources are kept and reused so that a failed or declined extraction can be
retried (ADR 0005). The daemon channels used that reuse for every repeat: a
message delivered twice paid for a second extraction, and any change in the
model's wording filed a near-duplicate note. `ingest_text` now stops when the
ledger shows an earlier `ingest.applied` for the identical source.
Real files under tmp_vault; only the model call is replaced.
"""

from __future__ import annotations

import pytest
from rich.console import Console

from sabnyx import ledger, services, sources
from sabnyx.schemas import IngestChangeset, ProposedIntake

DESCRIPTION = "Notes from a meeting, long enough to satisfy the schema's minimum."


def _model(monkeypatch) -> list[dict]:
    """Each call extracts a differently worded note, as a real model may."""
    calls: list[dict] = []

    def fake(**kwargs):
        calls.append(kwargs)
        note = ProposedIntake(
            type="note",
            title=f"Budget notes {len(calls)}",
            description=DESCRIPTION,
            tags=[],
            body=f"wording {len(calls)}",
        )
        return IngestChangeset(summary="budget captured.", intake_files=[note])

    monkeypatch.setattr(services.llm, "parse_structured", fake)
    return calls


def _ingest(text: str = "monday we set the budget", url: str | None = None):
    return services.ingest_text(
        text=text,
        title=url or "Budget",
        source_kind="web" if url else "manual",
        source_url=url,
        console=Console(quiet=True),
    )


def _kinds() -> list[str]:
    return [e.kind for e in ledger.replay()]


def test_identical_source_is_not_extracted_twice(tmp_vault, monkeypatch):
    calls = _model(monkeypatch)
    _summary, written = _ingest()

    summary, written_again = _ingest()

    assert len(calls) == 1
    assert written_again == []
    assert "already ingested" in summary
    assert written[0] in summary  # the reply names what the first run filed
    assert [p.name for p in tmp_vault["INTAKE"].glob("*.md")] == ["budget-notes-1.md"]
    assert _kinds().count("ingest.applied") == 1
    skipped = [e for e in ledger.replay() if e.kind == "ingest.skipped"]
    assert [e.payload["reason"] for e in skipped] == ["already_applied"]
    assert skipped[0].subject == "vault/sources/budget.md"


def test_changed_content_is_ingested_again(tmp_vault, monkeypatch):
    """Same URL, different page text: a new source, so a new extraction."""
    calls = _model(monkeypatch)
    _ingest("page, first version", url="https://example.net/page")

    _summary, written = _ingest("page, second version", url="https://example.net/page")

    assert len(calls) == 2
    assert written == ["vault/intake/budget-notes-2.md"]
    assert "ingest.skipped" not in _kinds()


def test_new_text_under_a_freed_source_name_is_extracted(tmp_vault, monkeypatch):
    """The earlier ingest is found by the source's file name. If that file
    was removed and a different text takes the name, the old `ingest.applied`
    is about other bytes: only an apply after the latest capture counts."""
    calls = _model(monkeypatch)
    _ingest("monday we set the budget")
    (tmp_vault["SOURCES"] / "budget.md").unlink()

    summary, written = _ingest("tuesday we cut the budget")

    assert len(calls) == 2
    assert "already ingested" not in summary
    assert written == ["vault/intake/budget-notes-2.md"]
    assert "ingest.skipped" not in _kinds()

    _ingest("tuesday we cut the budget")  # and the new text is itself not repeated
    assert len(calls) == 2


def test_failed_extraction_can_still_be_retried(tmp_vault, monkeypatch):
    """ADR 0005: the retained source of a failed extraction is reused, not
    refused. Only a successful earlier ingest stops a repeat."""

    def boom(**kwargs):
        raise RuntimeError("provider exploded")

    monkeypatch.setattr(services.llm, "parse_structured", boom)
    with pytest.raises(RuntimeError):
        _ingest()
    calls = _model(monkeypatch)

    _summary, written = _ingest()

    assert len(calls) == 1
    assert written == ["vault/intake/budget-notes-1.md"]
    assert _kinds().count("ingest.failed") == 1
    assert "ingest.skipped" not in _kinds()


@pytest.mark.parametrize(
    ("url", "normal"),
    [
        ("HTTPS://Example.NET/Page?b=2&a=1", "https://example.net/Page?b=2&a=1"),
        ("https://example.net", "https://example.net/"),
        ("https://example.net:443/x", "https://example.net/x"),
        ("http://example.net:80/x", "http://example.net/x"),
        ("http://example.net:8080/x", "http://example.net:8080/x"),
        ("https://example.net/x#section", "https://example.net/x"),
        ("https://example.net/x/", "https://example.net/x/"),
        (
            "https://example.net/x?utm_source=n&id=7&fbclid=abc&UTM_Medium=m",
            "https://example.net/x?id=7",
        ),
        ("https://example.net/x?utm_source=n", "https://example.net/x"),
        ("https://example.net/x?q=a%20b+c", "https://example.net/x?q=a%20b+c"),
        ("https://user@Example.net/x", "https://user@example.net/x"),
        ("https://[2001:DB8::1]:443/x", "https://[2001:db8::1]/x"),
    ],
)
def test_normalize_url(url, normal):
    assert sources.normalize_url(url) == normal
    assert sources.normalize_url(normal) == normal


def test_telegram_url_variants_are_one_source(tmp_vault, monkeypatch):
    """The same page sent with a tracking parameter and a fragment is fetched
    and stored under one URL, so the second message is recognised."""
    from sabnyx.adapters import telegram
    from sabnyx.commands import serve

    calls = _model(monkeypatch)
    fetched: list[str] = []

    def fetch(url: str) -> str:
        fetched.append(url)
        return "the page"

    monkeypatch.setattr(serve, "telegram_fetch", fetch)
    sent: list[str] = []
    monkeypatch.setattr(telegram, "send_message", lambda chat, text: sent.append(text))
    urls = ["https://Example.net/post?utm_source=feed", "https://example.net/post#top"]
    updates = [
        {
            "update_id": i,
            "message": {
                "message_id": i,
                "date": 1_700_000_000,
                "text": url,
                "chat": {"id": 999},
                "from": {"id": 555, "username": "op"},
            },
        }
        for i, url in enumerate(urls, 1)
    ]

    serve._dispatch_batch(updates, {"555"})

    assert fetched == ["https://example.net/post"] * 2
    assert len(calls) == 1
    assert "already ingested" in sent[1]
    assert len(list(tmp_vault["SOURCES"].glob("*.md"))) == 1
