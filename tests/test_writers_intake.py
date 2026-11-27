"""write_intake when the title's filename is already taken.

A taken name is not a reason to lose an extraction: a capture whose text
differs from the file holding the name is written under the first free `-N`;
one that repeats it is skipped, ledgered as `intake.skipped`, and reported to
the caller, so no channel can answer "filed" or stay silent about it.
Real files under tmp_vault; the ledger is read back with ledger.replay().
"""

from __future__ import annotations

from rich.console import Console

from sabnyx import ledger, paths, services
from sabnyx.adapters import telegram
from sabnyx.schemas import AskAnswer, IngestChangeset, ProposedIntake
from sabnyx.writers import write_intake

QUIET = Console(quiet=True)
DESCRIPTION = "Notes from a meeting, long enough to satisfy the schema's minimum."


def _pi(body: str, title: str = "Meeting notes") -> ProposedIntake:
    return ProposedIntake(type="note", title=title, description=DESCRIPTION, tags=[], body=body)


def _write(body: str, trailer: str | None = None):
    return write_intake(
        _pi(body), actor="sabnyx-ingest", source="manual", trailer=trailer, console=QUIET
    )


def _intake_names(tmp_vault) -> list[str]:
    return sorted(p.name for p in tmp_vault["INTAKE"].glob("*.md"))


def _events(kind: str) -> list:
    return [e for e in ledger.replay() if e.kind == kind]


def test_same_title_different_text_takes_the_next_free_name(tmp_vault):
    first = _write("monday: budget")
    second = _write("tuesday: hiring")
    third = _write("wednesday: roadmap")

    assert _intake_names(tmp_vault) == [
        "meeting-notes-2.md",
        "meeting-notes-3.md",
        "meeting-notes.md",
    ]
    assert "tuesday: hiring" in (tmp_vault["INTAKE"] / "meeting-notes-2.md").read_text()
    assert "monday: budget" in (tmp_vault["INTAKE"] / "meeting-notes.md").read_text()
    assert [o.kind for o in (first, second, third)] == ["written"] * 3
    assert [e.subject for e in _events("intake.created")] == [o.rel for o in (first, second, third)]
    assert _events("intake.skipped") == []


def test_same_title_same_text_is_skipped_and_ledgered(tmp_vault):
    """The provenance trailer names the source, so it differs between two
    captures of the same text; it is not part of the comparison."""
    first = _write("monday: budget", trailer="<!-- source-id:sabnyx-sources:a.md -->")
    again = _write("  monday: budget\n", trailer="<!-- source-id:sabnyx-sources:b.md -->")

    assert _intake_names(tmp_vault) == ["meeting-notes.md"]
    assert again.kind == "exists"
    assert again.rel == first.rel
    assert len(_events("intake.created")) == 1
    (skipped,) = _events("intake.skipped")
    assert skipped.subject == first.rel
    assert skipped.payload == {"reason": "duplicate", "slug": "meeting-notes"}


def test_a_repeat_of_a_suffixed_file_is_skipped_too(tmp_vault):
    _write("monday: budget")
    second = _write("tuesday: hiring")
    again = _write("tuesday: hiring")

    assert _intake_names(tmp_vault) == ["meeting-notes-2.md", "meeting-notes.md"]
    assert (again.kind, again.rel) == ("exists", second.rel)


# -- the channels say what happened ---------------------------------------------------


def _update(uid: int, text: str) -> dict:
    return {
        "update_id": uid,
        "message": {
            "message_id": uid,
            "date": 1_700_000_000,
            "text": text,
            "chat": {"id": 999},
            "from": {"id": 555, "username": "op"},
        },
    }


def _telegram(monkeypatch, bodies: list[str], texts: list[str]) -> list[str]:
    """Send `texts` as trusted Telegram messages; the model extracts
    'Meeting notes' with the next of `bodies` for each."""
    from sabnyx import llm
    from sabnyx.commands import serve

    queue = iter(bodies)

    def fake(**kwargs):
        assert kwargs["output_format"] is IngestChangeset
        return IngestChangeset(summary="meeting captured.", intake_files=[_pi(next(queue))])

    monkeypatch.setattr(llm, "parse_structured", fake)
    sent: list[str] = []
    monkeypatch.setattr(telegram, "send_message", lambda chat, text: sent.append(text))
    serve._dispatch_batch([_update(i, t) for i, t in enumerate(texts, 1)], {"555"})
    return sent


def test_telegram_files_a_second_capture_with_the_same_title(tmp_vault, monkeypatch):
    sent = _telegram(
        monkeypatch,
        ["monday: budget", "tuesday: hiring"],
        ["remember monday we set the budget", "remember tuesday we talked hiring"],
    )

    assert _intake_names(tmp_vault) == ["meeting-notes-2.md", "meeting-notes.md"]
    assert "filed 1: vault/intake/meeting-notes-2.md" in sent[1]


def test_telegram_says_when_a_capture_repeats_a_filed_one(tmp_vault, monkeypatch):
    sent = _telegram(
        monkeypatch,
        ["monday: budget", "monday: budget"],
        ["remember monday we set the budget", "remember on monday the budget was set"],
    )

    assert _intake_names(tmp_vault) == ["meeting-notes.md"]
    assert "filed 1: vault/intake/meeting-notes.md" in sent[0]
    assert "filed 1" not in sent[1]
    assert "already covered" not in sent[1]
    assert "not filed again: same text as vault/intake/meeting-notes.md" in sent[1]
    assert len(_events("intake.skipped")) == 1


def test_telegram_ask_says_when_the_capture_is_already_filed(tmp_vault, monkeypatch):
    from sabnyx import llm
    from sabnyx.commands import serve

    answer = AskAnswer(answer="Budget was set monday.", cited=[], capture=_pi("monday: budget"))
    monkeypatch.setattr(llm, "parse_structured", lambda **kw: answer)
    sent: list[str] = []
    monkeypatch.setattr(telegram, "send_message", lambda chat, text: sent.append(text))
    serve._dispatch_batch([_update(1, "what about monday?"), _update(2, "and monday?")], {"555"})

    assert _intake_names(tmp_vault) == ["meeting-notes.md"]
    assert "(captured to vault/intake/meeting-notes.md)" in sent[0]
    assert "captured to" not in sent[1]
    assert "(already captured as vault/intake/meeting-notes.md)" in sent[1]
    assert paths.KNOWLEDGE_LOG.read_text().count("ask-capture") == 1


def test_capture_answer_files_a_different_answer_under_the_same_title(tmp_vault):
    first = services.capture_answer(_pi("monday: budget"), question="q1?", console=QUIET)
    second = services.capture_answer(_pi("tuesday: hiring"), question="q2?", console=QUIET)

    assert (first.kind, second.kind) == ("written", "written")
    assert _intake_names(tmp_vault) == ["meeting-notes-2.md", "meeting-notes.md"]
    assert paths.KNOWLEDGE_LOG.read_text().count("ask-capture") == 2
