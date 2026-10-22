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
    # non-ASCII token: clean deny, not hmac.compare_digest's TypeError (pre-auth path)
    assert r1.check_bearer("Bearer café", trusted) is False
    assert r1.check_bearer("Bearer café", {"café"}) is True  # non-ASCII keys still match


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
    status, body, _ = serve_http.route(
        "POST", "/ingest", b'{"text":"secret"}', "Bearer nope", TRUSTED
    )
    assert status == 401
    assert body["error"] == "unauthorized"
    # nothing written, nothing ledgered
    assert not list(tmp_vault["INTAKE"].glob("*.md"))
    assert "wire.received" not in [e.kind for e in ledger.replay()]


def test_route_ingest_writes_and_ledgers(tmp_vault, monkeypatch):
    from sabnyx.commands import serve_http

    _fake_llm(monkeypatch)
    status, body, _ = serve_http.route(
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
    status, body, _ = serve_http.route(
        "POST", "/ask", b'{"text":"is the sky blue?"}', AUTH, TRUSTED
    )
    assert status == 200
    assert "sky is blue" in body["text"]
    assert body["cited"] == ["sky"]
    assert "ask.answered" in [e.kind for e in ledger.replay()]


def test_route_status(tmp_vault):
    from sabnyx.commands import serve_http

    status, body, _ = serve_http.route("GET", "/status", b"", AUTH, TRUSTED)
    assert status == 200
    assert set(body) == {"intake", "atlas", "observations"}
    assert all(isinstance(v, int) for v in body.values())


def test_route_status_trailing_slash(tmp_vault):
    from sabnyx.commands import serve_http

    status, _, _ = serve_http.route("GET", "/status/", b"", AUTH, TRUSTED)
    assert status == 200


def test_route_missing_text_is_400(tmp_vault):
    from sabnyx.commands import serve_http

    status, body, _ = serve_http.route("POST", "/ingest", b"{}", AUTH, TRUSTED)
    assert status == 400
    assert "text" in body["error"]


def test_route_bad_json_is_400(tmp_vault):
    from sabnyx.commands import serve_http

    status, _, _ = serve_http.route("POST", "/ask", b"not json", AUTH, TRUSTED)
    assert status == 400


def test_route_non_string_text_is_400(tmp_vault):
    from sabnyx.commands import serve_http

    # a JSON number/list/object for `text` must not reach `.strip()` and crash
    status, body, _ = serve_http.route("POST", "/ingest", b'{"text": 123}', AUTH, TRUSTED)
    assert status == 400
    assert "string" in body["error"]


def test_route_action_failure_returns_generic_error(tmp_vault, monkeypatch):
    """A failing action returns a generic 500 (no exception text leaked), while
    the real detail is ledgered for operators."""
    from sabnyx.commands import serve_http

    def boom(_text):
        raise RuntimeError("/home/USER/sabnyx-vault leaked secret path")

    monkeypatch.setattr(serve_http, "_ask", boom)
    status, body, _ = serve_http.route("POST", "/ask", b'{"text":"hi"}', AUTH, TRUSTED)
    assert status == 500
    assert body == {"error": "internal error"}  # nothing from the exception text
    errs = [e for e in ledger.replay() if e.kind == "wire.errored"]
    assert errs  # the failure is ledgered
    assert "leaked secret path" in errs[-1].payload["error"]  # detail kept internally


def test_route_unknown_path_is_404(tmp_vault):
    from sabnyx.commands import serve_http

    status, _, _ = serve_http.route("GET", "/nope", b"", AUTH, TRUSTED)
    assert status == 404


def test_route_wrong_method_is_404(tmp_vault):
    from sabnyx.commands import serve_http

    status, _, _ = serve_http.route("GET", "/ask", b"", AUTH, TRUSTED)
    assert status == 404


def test_serve_http_refuses_open(tmp_vault, monkeypatch):
    from sabnyx.commands import serve_http

    monkeypatch.delenv("SABNYX_R1_KEYS", raising=False)
    result = CliRunner().invoke(serve_http.serve_http_cmd, [])
    assert result.exit_code == 2
    # normalize whitespace: rich may wrap the warning across a line break
    assert "Refusing to run open" in " ".join(result.output.split())


# -- static serving (--static) -------------------------------------------------


def _make_creations(root):
    (root / "js").mkdir(parents=True)
    (root / "index.html").write_text("<html>sabnyx</html>")
    (root / "install.html").write_text("install")
    (root / "js" / "api.js").write_text("// api")
    return root


def test_resolve_static_maps_files(tmp_path):
    from sabnyx.commands import serve_http

    root = _make_creations(tmp_path / "creations")
    assert serve_http.resolve_static("/", root) == (root / "index.html").resolve()
    assert serve_http.resolve_static("/index.html", root) == (root / "index.html").resolve()
    assert serve_http.resolve_static("/install.html", root) == (root / "install.html").resolve()
    assert serve_http.resolve_static("/js/api.js", root) == (root / "js" / "api.js").resolve()
    # query string is stripped
    assert serve_http.resolve_static("/js/api.js?v=2", root) == (root / "js" / "api.js").resolve()


def test_resolve_static_rejects_missing_and_traversal(tmp_path):
    from sabnyx.commands import serve_http

    root = _make_creations(tmp_path / "creations")
    secret = tmp_path / "secret"  # sibling of root, outside it
    secret.write_text("TOPSECRET")

    assert serve_http.resolve_static("/nope.html", root) is None  # missing
    assert serve_http.resolve_static("/../secret", root) is None  # traversal, even though it exists
    assert serve_http.resolve_static("/js/../../secret", root) is None
    assert serve_http.resolve_static("/js", root) is None  # a directory, not a file


def test_resolve_static_rejects_nul_byte(tmp_path):
    """A raw NUL byte in the path must be None, not Path.resolve()'s
    ValueError('embedded null byte') — this runs unauthenticated."""
    from sabnyx.commands import serve_http

    root = _make_creations(tmp_path / "creations")
    assert serve_http.resolve_static("/\x00evil", root) is None
    assert serve_http.resolve_static("/js/\x00api.js", root) is None


def test_resolve_static_rejects_overlong_name(tmp_path):
    """A path component over the filesystem name limit raises OSError
    (ENAMETOOLONG) from is_file()'s stat, not resolve() — the guard must catch
    it so an unauthenticated GET can't traceback and reset the connection."""
    from sabnyx.commands import serve_http

    root = _make_creations(tmp_path / "creations")
    assert serve_http.resolve_static("/" + "a" * 256, root) is None
    assert serve_http.resolve_static("/js/" + "a" * 512 + ".js", root) is None


def test_static_public_while_api_gated_over_socket(tmp_vault):
    """End to end over a real socket: static is served unauthenticated, the API
    still requires the bearer key, and traversal can't escape the root."""
    import http.client
    import threading
    from http.server import ThreadingHTTPServer

    from sabnyx.commands import serve_http

    root = _make_creations(tmp_vault["DATA_ROOT"] / "creations")
    srv = ThreadingHTTPServer(("127.0.0.1", 0), serve_http._make_handler({"k1"}, root.resolve()))
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:

        def get(path, auth=None):
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            c.request("GET", path, headers=({"Authorization": auth} if auth else {}))
            r = c.getresponse()
            body = r.read()
            c.close()
            return r.status, body

        assert get("/") == (200, b"<html>sabnyx</html>")  # static public, no key
        assert get("/status")[0] == 401  # API gated
        assert get("/status", "Bearer k1")[0] == 200  # API with key
        assert get("/../../etc/passwd")[0] != 200  # traversal blocked
    finally:
        srv.shutdown()
        srv.server_close()


def test_post_content_length_guards_over_socket(tmp_vault):
    """do_POST validates Content-Length: a negative value must not bypass the cap
    into an unbounded read, a non-numeric value must not crash, and an oversized
    value is 413 — all answered without reading a body or hanging."""
    import socket
    import threading
    from http.server import ThreadingHTTPServer

    from sabnyx.commands import serve_http

    srv = ThreadingHTTPServer(("127.0.0.1", 0), serve_http._make_handler({"k1"}, None))
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:

        def status_line(content_length: str) -> bytes:
            c = socket.create_connection(("127.0.0.1", port), timeout=5)
            req = (
                "POST /ingest HTTP/1.1\r\n"
                "Host: localhost\r\n"
                "Authorization: Bearer k1\r\n"
                f"Content-Length: {content_length}\r\n"
                "Connection: close\r\n\r\n"
            ).encode()
            c.sendall(req)  # no body sent; the guard must answer before reading one
            first = c.recv(256).split(b"\r\n", 1)[0]
            c.close()
            return first

        assert b"400" in status_line("-1")  # negative: no unbounded read(-1)
        assert b"400" in status_line("abc")  # non-numeric: no uncaught ValueError
        assert b"413" in status_line(str(serve_http._MAX_BODY + 1))  # oversized
    finally:
        srv.shutdown()
        srv.server_close()


def test_rejects_transfer_encoding_over_socket(tmp_vault):
    """A request framed with Transfer-Encoding is refused (400) on both GET and
    POST — http.server can't decode chunked, and honoring it invites a CL/TE
    smuggling desync behind a proxy."""
    import socket
    import threading
    from http.server import ThreadingHTTPServer

    from sabnyx.commands import serve_http

    srv = ThreadingHTTPServer(("127.0.0.1", 0), serve_http._make_handler({"k1"}, None))
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:

        def status_line(method: str) -> bytes:
            c = socket.create_connection(("127.0.0.1", port), timeout=5)
            req = (
                f"{method} /status HTTP/1.1\r\n"
                "Host: localhost\r\n"
                "Authorization: Bearer k1\r\n"
                "Transfer-Encoding: chunked\r\n"
                "Connection: close\r\n\r\n"
            ).encode()
            c.sendall(req)
            first = c.recv(256).split(b"\r\n", 1)[0]
            c.close()
            return first

        assert b"400" in status_line("GET")
        assert b"400" in status_line("POST")
    finally:
        srv.shutdown()
        srv.server_close()


def test_hostile_bytes_get_clean_errors_over_socket(tmp_vault):
    """Unauthenticated hostile bytes must yield clean HTTP errors, never a
    handler traceback + connection reset: a non-ASCII bearer token used to hit
    hmac.compare_digest's TypeError inside route(), and a raw NUL byte in a
    static path used to hit Path.resolve()'s ValueError — both pre-auth."""
    import socket
    import threading
    from http.server import ThreadingHTTPServer

    from sabnyx.commands import serve_http

    root = _make_creations(tmp_vault["DATA_ROOT"] / "creations")
    srv = ThreadingHTTPServer(("127.0.0.1", 0), serve_http._make_handler({"k1"}, root.resolve()))
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:

        def status_line(req: bytes) -> bytes:
            c = socket.create_connection(("127.0.0.1", port), timeout=5)
            c.sendall(req)
            first = c.recv(256).split(b"\r\n", 1)[0]  # b"" if the handler crashed
            c.close()
            return first

        # non-ASCII bearer token (latin-1 é on the wire) -> clean 401, GET and POST
        assert b"401" in status_line(
            b"GET /status HTTP/1.1\r\nHost: x\r\n"
            b"Authorization: Bearer caf\xe9\r\nConnection: close\r\n\r\n"
        )
        assert b"401" in status_line(
            b"POST /ingest HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer caf\xe9\r\n"
            b"Content-Length: 0\r\nConnection: close\r\n\r\n"
        )
        # raw NUL byte in a static path -> clean 404, not an embedded-null crash
        assert b"404" in status_line(
            b"GET /\x00evil HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n"
        )
        # over-long static path -> clean 404, not an is_file() ENAMETOOLONG reset
        assert b"404" in status_line(
            b"GET /" + b"a" * 300 + b" HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n"
        )
    finally:
        srv.shutdown()
        srv.server_close()
