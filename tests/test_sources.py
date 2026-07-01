"""SSRF guard + fetch hardening for sources.fetch_url.

DNS is stubbed so these never touch the network: _fake_getaddrinfo maps a host
to a fixed IP, exercising _guard_url's accept/reject logic deterministically.
"""

from __future__ import annotations

import socket

import pytest

from sabnyx import sources


def _stub_dns(monkeypatch, ip: str) -> None:
    def _fake_getaddrinfo(host, port, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, port))]

    monkeypatch.setattr(sources.socket, "getaddrinfo", _fake_getaddrinfo)


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://example.com/x",
        "gopher://example.com/",
        "data:text/plain,hi",
        "//example.com/x",  # scheme-relative -> no scheme
    ],
)
def test_guard_rejects_non_http_schemes(url: str) -> None:
    with pytest.raises(ValueError, match="scheme|host"):
        sources._guard_url(url)


@pytest.mark.parametrize(
    "ip",
    [
        "127.0.0.1",  # loopback
        "10.0.0.5",  # RFC-1918 private
        "192.168.1.10",  # RFC-1918 private
        "172.16.0.1",  # RFC-1918 private
        "169.254.169.254",  # link-local / cloud metadata (IMDS)
        "0.0.0.0",  # unspecified
        "224.0.0.1",  # multicast
        "100.64.0.1",  # CGNAT (RFC 6598) — not is_private, blocked explicitly
        "100.127.255.254",  # CGNAT upper edge
    ],
)
def test_guard_rejects_internal_addresses(monkeypatch, ip: str) -> None:
    _stub_dns(monkeypatch, ip)
    with pytest.raises(ValueError, match="internal/reserved"):
        sources._guard_url(f"http://attacker.example/{ip}")


def test_guard_allows_public_address(monkeypatch) -> None:
    _stub_dns(monkeypatch, "93.184.216.34")  # example.com, public
    # Must not raise.
    sources._guard_url("https://example.com/page")


def test_fetch_url_wraps_guard_failure_as_runtimeerror(monkeypatch) -> None:
    # A blocked URL surfaces as RuntimeError (callers like `ingest --url` don't
    # wrap it), never an uncaught ValueError or a real connection.
    _stub_dns(monkeypatch, "127.0.0.1")
    with pytest.raises(RuntimeError, match="could not fetch"):
        sources.fetch_url("http://localhost/secret")


def test_fetch_url_rejects_file_scheme_without_network(monkeypatch) -> None:
    # No DNS stub needed: scheme is rejected before resolution. If this ever
    # tried to open the file we'd know the guard regressed.
    def _boom(*a, **k):
        raise AssertionError("file:// must be rejected before any open()")

    monkeypatch.setattr(sources.urllib.request, "build_opener", _boom)
    with pytest.raises(RuntimeError, match="could not fetch"):
        sources.fetch_url("file:///etc/passwd")
