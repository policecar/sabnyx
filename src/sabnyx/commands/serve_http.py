"""`sabnyx serve-http` — drive the vault from the Rabbit R1 over HTTP.

A small stdlib `http.server` ingress: the R1 Creation POSTs a voice transcript
(plain text) with a bearer key; we route it through the same services.py write
path the CLI and Telegram use, and return JSON the Creation speaks back.

Purely additive — no Whisper (rabbit transcribes on-device), no vault changes,
the Telegram loop untouched. Auth mirrors serve's trusted-sender model: a
bearer-key allowlist, and like serve we refuse to run open (empty allowlist).

Routes are explicit — the path IS the action, no LLM routing needed:
    POST /ingest  {text}  -> capture text into intake/ -> {text, filed:[...]}
    POST /ask     {text}  -> grounded answer            -> {text, cited:[...]}
    GET  /status          -> vault pulse                -> {intake, atlas, observations}

`route()` is a pure dispatcher (auth -> route -> act -> JSON) with no sockets,
so the HTTP handler and the tests both call it. Every request maps to a
NormalizedEnvelope(source="web") for ledger provenance.

@decision DEC-01: route() is socketless and the handler is a thin shim
@rationale: mirrors serve.py's testable _dispatch_batch — all behavior (auth,
  routing, errors) is unit-tested without binding a port; the BaseHTTPRequestHandler
  only translates bytes<->route() and is left untested (stdlib boundary).
"""

from __future__ import annotations

import io
import json
import mimetypes
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import click
from rich.console import Console

from .. import ledger, services
from ..adapters import r1
from ..spend import SpendCapExceeded
from ..util import VaultLocked, vault_lock

ACTOR = "sabnyx-r1"
_MAX_BODY = 64 * 1024  # generous for a voice transcript; reject anything larger


def _quiet() -> Console:
    """A throwaway console whose output is discarded.

    Built per request, not shared at module scope: the writers want a console
    but we don't want their output. A fresh StringIO each call avoids unbounded
    growth over the server's lifetime and keeps requests from sharing state.
    """
    return Console(file=io.StringIO())


def _status() -> dict:
    """A terse vault pulse for the R1 status line."""
    return services.vault_pulse()


def _ingest(text: str) -> dict:
    """Capture a transcript into intake/ via the shared services write path."""
    title = services.title_from(text)
    try:
        with vault_lock():
            head, written = services.ingest_text(
                text=text,
                title=title,
                source_kind="manual",
                source_url=None,
                console=_quiet(),
                actor=ACTOR,
            )
    except FileExistsError:
        return {"text": f"already captured something titled '{title}'.", "filed": []}
    return {"text": head, "filed": written}


def _ask(text: str) -> dict:
    """Answer a question grounded in the vault; auto-capture durable insight."""
    result = services.answer_question(question=text, actor=ACTOR)  # writes nothing
    out: dict = {"text": result.answer.answer, "cited": result.answer.cited}
    if result.answer.capture is not None:
        try:
            with vault_lock():
                rel = services.capture_answer(
                    result.answer.capture, question=text, console=_quiet(), actor=ACTOR
                )
        except VaultLocked:
            # The answer stands on its own — a busy lock just means the
            # (non-critical) auto-capture didn't happen this time.
            out["capture_error"] = "vault busy — not captured"
        else:
            if rel:
                out["captured"] = rel
    return out


def route(
    method: str, path: str, body: bytes, authorization: str | None, trusted: set[str]
) -> tuple[int, dict, dict[str, str]]:
    """Pure request dispatcher: auth, route, act. Returns (status, json_body, headers).

    No sockets — the HTTP handler and the tests both call this. A failing action
    is reported as a 500 JSON body and ledgered, never raised, so one bad request
    can't take the server down. `headers` is empty for every response except a
    429 (SpendCapExceeded), which carries a ``Retry-After`` value."""
    if not r1.check_bearer(authorization, trusted):
        return 401, {"error": "unauthorized"}, {}

    path = path.split("?", 1)[0].rstrip("/") or "/"

    if method == "GET" and path == "/status":
        env = r1.envelope_for_request("/status")
        ledger.append_event(
            plane="event",
            kind="wire.received",
            actor=ACTOR,
            subject=f"r1:{env.source_id}",
            payload={"action": "status"},
        )
        return 200, _status(), {}

    if method == "POST" and path in ("/ingest", "/ask"):
        try:
            data = json.loads(body.decode("utf-8")) if body else {}
        except (json.JSONDecodeError, UnicodeDecodeError):
            return 400, {"error": "invalid JSON body"}, {}
        if not isinstance(data, dict):
            return 400, {"error": "body must be a JSON object"}, {}
        raw = data.get("text")
        if raw is not None and not isinstance(raw, str):
            return 400, {"error": "'text' must be a string"}, {}
        text = (raw or "").strip()
        if not text:
            return 400, {"error": "missing 'text'"}, {}

        action = "ask" if path == "/ask" else "ingest"
        env = r1.envelope_for_request(text)
        ledger.append_event(
            plane="event",
            kind="wire.received",
            actor=ACTOR,
            subject=f"r1:{env.source_id}",
            payload={"action": action},
        )
        try:
            return 200, (_ask(text) if action == "ask" else _ingest(text)), {}
        except VaultLocked as e:
            # VaultLocked's message is already end-user-safe (no path/exception
            # detail) — unlike the generic 500 below, it's returned verbatim.
            ledger.append_event(
                plane="event",
                kind="wire.errored",
                actor=ACTOR,
                subject=f"r1:{env.source_id}",
                payload={"error": str(e), "busy": True},
            )
            return 503, {"error": str(e)}, {}
        except SpendCapExceeded as e:
            # End-user-safe (see spend.SpendCapExceeded); the client gets the
            # seconds to wait both in the body and as a Retry-After header.
            # No wire.errored write here, deliberately: spend.check_ingress_cap
            # already ledgers ingress.capped at most once per tripped window —
            # a second write per refused request would defeat that bound
            # under exactly the refusal-storm load the cap exists to survive.
            return 429, {"error": str(e)}, {"Retry-After": str(e.retry_after_seconds)}
        except Exception as e:  # noqa: BLE001 — report failure as JSON, never crash the loop
            # Detail goes to the ledger only; the client gets a generic message
            # so exception text (paths, provider errors) never leaks over the wire.
            ledger.append_event(
                plane="event",
                kind="wire.errored",
                actor=ACTOR,
                subject=f"r1:{env.source_id}",
                payload={"error": str(e)},
            )
            return 500, {"error": "internal error"}, {}

    return 404, {"error": "not found"}, {}


def resolve_static(url_path: str, root: Path) -> Path | None:
    """Map a URL path to a file under `root`, or None.

    Returns None for a missing file, a directory, or any path that escapes
    `root` (e.g. ``/../etc/passwd``) — the path-traversal guard. Pure beyond
    the final ``is_file()`` check."""
    rel = url_path.split("?", 1)[0].lstrip("/") or "index.html"
    if rel.endswith("/"):
        rel += "index.html"
    root = root.resolve()
    try:
        # .resolve() AND the final is_file() stat can raise on hostile paths
        # (ValueError for an embedded NUL byte, OSError for an over-long name) —
        # this runs unauthenticated, so treat all of those as "no such file".
        # is_file() must stay inside the guard: .resolve() on a non-existent
        # path resolves lexically without a stat, so the ENAMETOOLONG OSError
        # surfaces at is_file(), not above.
        candidate = (root / rel).resolve()
        candidate.relative_to(root)
        return candidate if candidate.is_file() else None
    except ValueError:
        return None  # escaped the static root, or unresolvable (NUL byte)
    except OSError:
        return None  # over-long or otherwise un-stat-able name


def _make_handler(
    trusted: set[str], static_root: Path | None = None
) -> type[BaseHTTPRequestHandler]:
    """Build a request handler bound to `trusted` (+ optional static root).

    API routes are bearer-gated via route(); when `static_root` is set, other
    GETs serve files from it WITHOUT auth — the page must load before the user
    has a key, and the static files hold no secrets."""

    class Handler(BaseHTTPRequestHandler):
        server_version = "sabnyx-r1/0.1"

        def _send(self, status: int, ctype: str, body: bytes, extra: dict | None = None) -> None:
            """Write one response, tolerating a client that hung up mid-send.

            When nginx closes the upstream on its proxy timeout (or the device
            disconnects), the socket write raises BrokenPipeError/
            ConnectionResetError. That request already failed at the proxy — it's
            expected, not a bug, so swallow it instead of letting the stdlib dump
            a traceback per occurrence. Close the connection and move on."""
            try:
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                self.close_connection = True

        def _respond(
            self, status: int, payload: dict, headers: dict[str, str] | None = None
        ) -> None:
            self._send(status, "application/json", json.dumps(payload).encode("utf-8"), headers)

        def _file(self, path: Path) -> None:
            body = path.read_bytes()
            ctype = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
            self._send(200, ctype, body, {"Cache-Control": "no-cache"})

        def _reject_te(self) -> bool:
            """Reject any request that frames its body with Transfer-Encoding.

            This server only speaks Content-Length framing, and http.server does
            not actually decode chunked bodies — so honoring a TE header invites a
            request-smuggling desync when a front proxy and this backend disagree
            on where the body ends. Reject it and close the connection (an
            unconsumed chunked body would otherwise poison the keep-alive)."""
            if self.headers.get("Transfer-Encoding"):
                self.close_connection = True
                self._respond(400, {"error": "Transfer-Encoding not supported"})
                return True
            return False

        def do_GET(self) -> None:  # noqa: N802 — stdlib dispatch name
            if self._reject_te():
                return
            path = self.path.split("?", 1)[0].rstrip("/") or "/"
            if path == "/status":  # API route — bearer-gated
                status, payload, headers = route(
                    "GET", self.path, b"", self.headers.get("Authorization"), trusted
                )
                self._respond(status, payload, headers)
                return
            if static_root is not None:  # public static assets (no secrets)
                f = resolve_static(self.path, static_root)
                if f is not None:
                    self._file(f)
                    return
            self._respond(404, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802 — stdlib dispatch name
            if self._reject_te():
                return
            # Validate Content-Length before reading: a negative value would
            # otherwise skip the cap (-1 > _MAX_BODY is False) and turn into
            # rfile.read(-1) — an unbounded read until EOF; a non-numeric value
            # would raise an uncaught ValueError.
            try:
                length = int(self.headers.get("Content-Length", "0") or "0")
            except ValueError:
                self._respond(400, {"error": "invalid Content-Length"})
                return
            if length < 0:
                self._respond(400, {"error": "invalid Content-Length"})
                return
            if length > _MAX_BODY:
                self._respond(413, {"error": "body too large"})
                return
            body = self.rfile.read(length) if length else b""
            status, payload, headers = route(
                "POST", self.path, body, self.headers.get("Authorization"), trusted
            )
            self._respond(status, payload, headers)

        def log_message(self, format: str, *args: object) -> None:  # silence access log
            pass

    return Handler


@click.command("serve-http")
@click.option("--host", default="127.0.0.1", help="Bind address (put nginx/TLS in front).")
@click.option("--port", default=8787, help="Bind port.")
@click.option(
    "--static",
    "static_dir",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=None,
    help="Also serve a static dir (e.g. creations/) on the same origin — no CORS.",
)
def serve_http_cmd(host: str, port: int, static_dir: Path | None) -> None:
    """Run the Rabbit R1 HTTP ingress: authed, JSON in/out, over services.py.

    With --static, one process also serves the Creation's files, so the page and
    the API share one origin (CORS can't arise) and one unit covers both. API
    routes stay bearer-gated; static assets are public."""
    console = Console()

    trusted = r1.keys()
    if not trusted:
        console.print(
            "[yellow]warning:[/yellow] SABNYX_R1_KEYS is empty — every caller "
            "would be trusted. Refusing to run open."
        )
        raise SystemExit(2)

    from ..paths import ensure_tree

    ensure_tree()
    static_root = static_dir.resolve() if static_dir else None
    # Single-threaded: vault writes (sources.capture, the ledger append, git)
    # are not concurrency-safe, and a voice device may retry mid-request. One
    # request at a time trades a little latency for no write races.
    httpd = HTTPServer((host, port), _make_handler(trusted, static_root))
    where = f" + static {static_root}" if static_root else ""
    console.print(
        f"[green]serving R1 ingress[/green] on http://{host}:{port}{where} "
        f"(trusted keys: {len(trusted)}). ctrl-c to stop."
    )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        console.print("\n[dim]stopped.[/dim]")
    finally:
        httpd.server_close()
