"""Tests for the Telegram adapter and serve loop (no network)."""

from __future__ import annotations

from click.testing import CliRunner

from sabnyx import ledger
from sabnyx.adapters import telegram
from sabnyx.schemas import AskAnswer, IngestChangeset, ProposedIntake

# -- routing (pure) ----------------------------------------------------------------------


def test_classify():
    from sabnyx.commands import serve

    assert serve.classify("/ask what is x")[0] == "ask"
    assert serve.classify("/ask what is x")[1] == "what is x"
    assert serve.classify("is the sky blue?") == ("ask", "is the sky blue?")
    assert serve.classify("/status")[0] == "status"
    assert serve.classify("/health")[0] == "status"
    assert serve.classify("https://example.com/a") == ("ingest_url", "https://example.com/a")
    assert serve.classify("/remember buy milk")[0] == "ingest_text"
    assert serve.classify("/remember buy milk")[1] == "buy milk"
    assert serve.classify("a stray thought")[0] == "ingest_text"


def test_classify_greet():
    """/start and /help (and variants) route to 'greet', not ingest."""
    from sabnyx.commands import serve

    # Bare commands
    assert serve.classify("/start") == ("greet", "")
    assert serve.classify("/help") == ("greet", "")
    # Case-insensitive
    assert serve.classify("/START") == ("greet", "")
    assert serve.classify("/HELP") == ("greet", "")
    # Telegram @botname suffix
    assert serve.classify("/start@sabnyxbot") == ("greet", "")
    assert serve.classify("/help@sabnyxbot") == ("greet", "")
    # Deep-link payload after /start
    assert serve.classify("/start somedeeplink") == ("greet", "")
    assert serve.classify("/start@sabnyxbot somedeeplink") == ("greet", "")


def test_greet_does_not_capture(tmp_vault, monkeypatch):
    """/start returns greeting text and writes no source/intake files."""
    from sabnyx.commands import serve

    sent: list[tuple] = []
    monkeypatch.setattr(telegram, "send_message", lambda chat, text: sent.append((chat, text)))

    serve._dispatch_batch([_update(1, "/start")], {"555"})

    # Greeting reply sent
    assert len(sent) == 1
    assert "sabnyx" in sent[0][1]
    # No source file written
    assert not list(tmp_vault["SOURCES"].rglob("*.md"))
    # No intake file written
    assert not list(tmp_vault["INTAKE"].glob("*.md"))


def test_help_does_not_capture(tmp_vault, monkeypatch):
    """/help behaves identically to /start — greeting, no capture."""
    from sabnyx.commands import serve

    sent: list[tuple] = []
    monkeypatch.setattr(telegram, "send_message", lambda chat, text: sent.append((chat, text)))

    serve._dispatch_batch([_update(2, "/help")], {"555"})

    assert len(sent) == 1
    assert "sabnyx" in sent[0][1]
    assert not list(tmp_vault["SOURCES"].rglob("*.md"))
    assert not list(tmp_vault["INTAKE"].glob("*.md"))


def test_start_botname_suffix_does_not_capture(tmp_vault, monkeypatch):
    """/start@sabnyxbot routes to greeting, not capture."""
    from sabnyx.commands import serve

    sent: list[tuple] = []
    monkeypatch.setattr(telegram, "send_message", lambda chat, text: sent.append((chat, text)))

    serve._dispatch_batch([_update(3, "/start@sabnyxbot")], {"555"})

    assert len(sent) == 1
    assert "sabnyx" in sent[0][1]
    assert not list(tmp_vault["SOURCES"].rglob("*.md"))


def test_start_deeplink_does_not_capture(tmp_vault, monkeypatch):
    """/start <payload> routes to greeting, not capture."""
    from sabnyx.commands import serve

    sent: list[tuple] = []
    monkeypatch.setattr(telegram, "send_message", lambda chat, text: sent.append((chat, text)))

    serve._dispatch_batch([_update(4, "/start somedeeplink")], {"555"})

    assert len(sent) == 1
    assert "sabnyx" in sent[0][1]
    assert not list(tmp_vault["SOURCES"].rglob("*.md"))


def test_plain_text_still_captures(tmp_vault, monkeypatch):
    """Normal text still triggers the capture path (guard against over-matching)."""
    from sabnyx.commands import serve

    _fake_llm(monkeypatch)
    sent: list[tuple] = []
    monkeypatch.setattr(telegram, "send_message", lambda chat, text: sent.append((chat, text)))

    serve._dispatch_batch([_update(5, "remember buy milk")], {"555"})

    assert (tmp_vault["INTAKE"] / "buy-milk-reminder.md").exists()
    assert "filed 1" in sent[0][1]


# -- adapter normalization ----------------------------------------------------------------


def test_envelope_for_update_maps_message():
    update = {
        "update_id": 42,
        "message": {
            "message_id": 7,
            "date": 1_700_000_000,
            "text": "hello",
            "chat": {"id": 999},
            "from": {"id": 555, "username": "op"},
        },
    }
    env = telegram.envelope_for_update(update)
    assert env is not None
    assert env.source == "telegram"
    assert env.source_id == "42"
    assert env.actor_raw == "555"
    assert env.actor_canonical == "op"
    assert env.payload["text"] == "hello"
    assert env.payload["chat_id"] == 999


def test_envelope_for_update_ignores_non_text():
    assert telegram.envelope_for_update({"update_id": 1, "message": {"sticker": {}}}) is None
    assert telegram.envelope_for_update({"update_id": 2}) is None


# -- dispatch end to end (faked network + LLM) --------------------------------------------

INGEST_CS = IngestChangeset(
    summary="captured a thought about milk.",
    intake_files=[
        ProposedIntake(
            type="thought",
            title="Buy milk reminder",
            description="A reminder to buy milk, captured over telegram, long enough for schema.",
            tags=[],
            body="buy milk",
        )
    ],
    observations=[],
)

ASK_ANS = AskAnswer(
    answer="The sky is blue per [[sky]].",
    cited=["sky"],
    capture=None,
)


def _fake_llm(monkeypatch):
    from sabnyx import llm

    def fake(**kwargs):
        fmt = kwargs["output_format"]
        if fmt is IngestChangeset:
            return INGEST_CS
        if fmt is AskAnswer:
            return ASK_ANS
        raise AssertionError(fmt)

    monkeypatch.setattr(llm, "parse_structured", fake)


def _update(uid, text, from_id=555):
    return {
        "update_id": uid,
        "message": {
            "message_id": uid,
            "date": 1_700_000_000,
            "text": text,
            "chat": {"id": 999},
            "from": {"id": from_id, "username": "op"},
        },
    }


def test_dispatch_ingest_and_ask(tmp_vault, monkeypatch):
    from sabnyx.commands import serve

    _fake_llm(monkeypatch)
    sent: list[tuple] = []
    monkeypatch.setattr(telegram, "send_message", lambda chat, text: sent.append((chat, text)))

    serve._dispatch_batch(
        [_update(1, "remember buy milk"), _update(2, "is the sky blue?")], {"555"}
    )

    # ingest wrote an intake file; ask replied with the grounded answer
    assert (tmp_vault["INTAKE"] / "buy-milk-reminder.md").exists()
    assert len(sent) == 2
    assert "filed 1" in sent[0][1]
    assert "sky is blue" in sent[1][1]

    kinds = [e.kind for e in ledger.replay()]
    assert kinds.count("wire.received") == 2
    assert "source.captured" in kinds
    assert "intake.created" in kinds
    assert "ask.answered" in kinds


def test_dispatch_rejects_untrusted(tmp_vault, monkeypatch):
    from sabnyx.commands import serve

    _fake_llm(monkeypatch)
    sent: list[tuple] = []
    monkeypatch.setattr(telegram, "send_message", lambda chat, text: sent.append((chat, text)))

    serve._dispatch_batch([_update(1, "remember secret", from_id=666)], {"555"})

    assert sent == []  # no reply to an untrusted sender
    assert not list(tmp_vault["INTAKE"].glob("*.md"))
    kinds = [e.kind for e in ledger.replay()]
    assert "wire.rejected" in kinds
    assert "source.captured" not in kinds


def test_dispatch_offset_advances(tmp_vault, monkeypatch):
    from sabnyx.commands import serve

    _fake_llm(monkeypatch)
    monkeypatch.setattr(telegram, "send_message", lambda chat, text: None)
    offset = serve._dispatch_batch([_update(10, "/status"), _update(13, "/status")], {"555"})
    assert offset == 14  # max update_id + 1


def test_status_reply(tmp_vault, monkeypatch):
    from sabnyx.commands import serve

    sent: list[tuple] = []
    monkeypatch.setattr(telegram, "send_message", lambda chat, text: sent.append((chat, text)))
    serve._dispatch_batch([_update(1, "/status")], {"555"})
    assert "intake:" in sent[0][1]
    assert "atlas:" in sent[0][1]


def test_serve_refuses_open(tmp_vault, monkeypatch):
    from sabnyx.commands import serve

    monkeypatch.delenv("SABNYX_TELEGRAM_ALLOW", raising=False)
    result = CliRunner().invoke(serve.serve_cmd, ["--once"])
    assert result.exit_code == 2
    assert "Refusing to run open" in result.output


def test_serve_once_drains(tmp_vault, monkeypatch):
    from sabnyx.commands import serve

    monkeypatch.setenv("SABNYX_TELEGRAM_ALLOW", "555")
    _fake_llm(monkeypatch)
    monkeypatch.setattr(telegram, "get_updates", lambda offset, timeout=30: [_update(1, "/status")])
    sent: list[tuple] = []
    monkeypatch.setattr(telegram, "send_message", lambda chat, text: sent.append((chat, text)))

    result = CliRunner().invoke(serve.serve_cmd, ["--once"])
    assert result.exit_code == 0, result.output
    assert len(sent) == 1
