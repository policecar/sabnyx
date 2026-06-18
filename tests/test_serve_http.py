"""Tests for the Rabbit R1 adapter and HTTP ingress (no network/sockets).

Mirrors test_serve.py: the pure pieces (bearer auth, key parsing, envelope
mapping) are tested directly, and end-to-end routing goes through the socketless
`route()` dispatcher with a faked LLM — exactly as serve's _dispatch_batch is
tested without touching Telegram.
"""

from __future__ import annotations

from click.testing import CliRunner

from sabnyx import ledger
from sabnyx.adapters import r1
from sabnyx.schemas import AskAnswer, IngestChangeset, ProposedIntake

# -- auth + key parsing (pure) -------------------------------------------------


def test_check_bearer():
    trusted = {"key-aaa", "key-bbb"}
    assert r1.check_bearer("Bearer key-aaa", trusted) is True
    assert r1.check_bearer("Bearer key-bbb", trusted) is True
    assert r1.check_bearer("bearer key-aaa", trusted) is True  # scheme case-insensitive
    assert r1.check_bearer("Bearer wrong", trusted) is False
    assert r1.check_bearer("Token key-aaa", trusted) is False  # wrong scheme
    assert r1.check_bearer("key-aaa", trusted) is False  # no scheme
    assert r1.check_bearer("Bearer ", trusted) is False  # empty token
    assert r1.check_bearer("", trusted) is False
    assert r1.check_bearer(None, trusted) is False
    assert r1.check_bearer("Bearer key-aaa", set()) is False  # empty allowlist denies


def test_keys_parsing(monkeypatch):
    monkeypatch.setenv("SABNYX_R1_KEYS", " a , b ,, c ")
    assert r1.keys() == {"a", "b", "c"}
    monkeypatch.delenv("SABNYX_R1_KEYS", raising=False)
    assert r1.keys() == set()


# -- envelope normalization ----------------------------------------------------


def test_envelope_for_request_maps_web():
    env = r1.envelope_for_request("hello there", request_id="req-1")
    assert env.source == "web"
    assert env.source_id == "req-1"
    assert env.actor_raw == "r1"
    assert env.kind == "message.received"
    assert env.payload["text"] == "hello there"


def test_envelope_request_id_is_content_stable():
    a = r1.envelope_for_request("same text")
    b = r1.envelope_for_request("same text")
    assert a.source_id == b.source_id  # content hash -> dedups retries


# -- route dispatch end to end (faked LLM) -------------------------------------

INGEST_CS = IngestChangeset(
    summary="captured a thought about milk.",
    intake_files=[
        ProposedIntake(
            type="thought",
            title="Buy milk reminder",
            description="A reminder to buy milk, captured over the R1, long enough for schema.",
            tags=[],
            body="buy milk",
        )
    ],
    observations=[],
)

ASK_ANS = AskAnswer(answer="The sky is blue per [[sky]].", cited=["sky"], capture=None)

TRUSTED = {"k1"}
AUTH = "Bearer k1"


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


def test_route_rejects_missing_or_bad_auth(tmp_vault, monkeypatch):
    from sabnyx.commands import serve_http

    _fake_llm(monkeypatch)
    status, body = serve_http.route("POST", "/ingest", b'{"text":"secret"}', "Bearer nope", TRUSTED)
    assert status == 401
    assert body["error"] == "unauthorized"
    # nothing written, nothing ledgered
    assert not list(tmp_vault["INTAKE"].glob("*.md"))
    assert "wire.received" not in [e.kind for e in ledger.replay()]


def test_route_ingest_writes_and_ledgers(tmp_vault, monkeypatch):
    from sabnyx.commands import serve_http

    _fake_llm(monkeypatch)
    status, body = serve_http.route(
        "POST", "/ingest", b'{"text":"remember buy milk"}', AUTH, TRUSTED
    )
    assert status == 200
    assert body["filed"] == ["vault/intake/buy-milk-reminder.md"]  # DATA_ROOT-relative
    assert (tmp_vault["INTAKE"] / "buy-milk-reminder.md").exists()

    kinds = [e.kind for e in ledger.replay()]
    assert "wire.received" in kinds
    assert "source.captured" in kinds
    assert "intake.created" in kinds


def test_route_ask_returns_answer_and_citations(tmp_vault, monkeypatch):
    from sabnyx.commands import serve_http

    _fake_llm(monkeypatch)
    status, body = serve_http.route("POST", "/ask", b'{"text":"is the sky blue?"}', AUTH, TRUSTED)
    assert status == 200
    assert "sky is blue" in body["text"]
    assert body["cited"] == ["sky"]
    assert "ask.answered" in [e.kind for e in ledger.replay()]


def test_route_status(tmp_vault):
    from sabnyx.commands import serve_http

    status, body = serve_http.route("GET", "/status", b"", AUTH, TRUSTED)
    assert status == 200
    assert set(body) == {"intake", "atlas", "observations"}
    assert all(isinstance(v, int) for v in body.values())


def test_route_status_trailing_slash(tmp_vault):
    from sabnyx.commands import serve_http

    status, _ = serve_http.route("GET", "/status/", b"", AUTH, TRUSTED)
    assert status == 200


def test_route_missing_text_is_400(tmp_vault):
    from sabnyx.commands import serve_http

    status, body = serve_http.route("POST", "/ingest", b"{}", AUTH, TRUSTED)
    assert status == 400
    assert "text" in body["error"]


def test_route_bad_json_is_400(tmp_vault):
    from sabnyx.commands import serve_http

    status, _ = serve_http.route("POST", "/ask", b"not json", AUTH, TRUSTED)
    assert status == 400


def test_route_unknown_path_is_404(tmp_vault):
    from sabnyx.commands import serve_http

    status, _ = serve_http.route("GET", "/nope", b"", AUTH, TRUSTED)
    assert status == 404


def test_route_wrong_method_is_404(tmp_vault):
    from sabnyx.commands import serve_http

    status, _ = serve_http.route("GET", "/ask", b"", AUTH, TRUSTED)
    assert status == 404


def test_serve_http_refuses_open(tmp_vault, monkeypatch):
    from sabnyx.commands import serve_http

    monkeypatch.delenv("SABNYX_R1_KEYS", raising=False)
    result = CliRunner().invoke(serve_http.serve_http_cmd, [])
    assert result.exit_code == 2
    # normalize whitespace: rich may wrap the warning across a line break
    assert "Refusing to run open" in " ".join(result.output.split())
