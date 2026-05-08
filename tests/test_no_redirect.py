"""F2: HTTP 30x redirects must be rejected — prevents signed headers leaking cross-domain.

Running a local HTTPS server is impractical (would need a cert), so we
directly verify the handler class's behavior: calling
`_NoRedirectHandler.http_error_30X` must raise EmgError rather than return a
new Request.

Plus a real-socket redirect smoke test (uses http://localhost to short-circuit
the HTTPS-enforcement check; tests that the opener pipeline really blocks
the redirect, given _NoRedirectHandler is installed).
"""

import http.server
import io
import os
import socket
import threading
import urllib.request

import pytest

from lib.govnet_lib import EmgError, _NoRedirectHandler, _OPENER


# --- Unit layer: invoke the handler class directly ---------------------------------------------


@pytest.mark.parametrize("code,method", [
    (301, "http_error_301"),
    (302, "http_error_302"),
    (303, "http_error_303"),
    (307, "http_error_307"),
    (308, "http_error_308"),
])
def test_handler_raises_on_each_redirect_code(code, method):
    handler = _NoRedirectHandler()
    fake_headers = {"Location": "https://attacker.example/v1/orders"}
    fake_req = urllib.request.Request("https://api.gov.works/v1/orders")
    fake_fp = io.BytesIO(b"")

    with pytest.raises(EmgError) as excinfo:
        getattr(handler, method)(fake_req, fake_fp, code, "Found", fake_headers)
    assert excinfo.value.code == "INSECURE_REDIRECT"
    assert excinfo.value.status == code
    assert "attacker.example" in excinfo.value.detail


def test_handler_error_includes_target_when_no_location_header():
    handler = _NoRedirectHandler()
    fake_req = urllib.request.Request("http://example.com")
    fake_fp = io.BytesIO(b"")
    with pytest.raises(EmgError) as excinfo:
        handler.http_error_302(fake_req, fake_fp, 302, "Found", {})
    # Without a Location header, detail writes "?"
    assert "?" in excinfo.value.detail


# --- Integration layer: spin up a mini HTTP server and verify _OPENER really doesn't follow -----------------


class _RedirectingHandler(http.server.BaseHTTPRequestHandler):
    """On any GET, return a 302 → a completely different target."""
    def do_GET(self):
        self.send_response(302)
        self.send_header("Location", "http://attacker.local/never-follow-me")
        self.end_headers()

    def log_message(self, *args, **kwargs):
        pass  # silence


def _start_server():
    """Use port 0 to let the OS assign an unused port; returns (server, port)."""
    server = http.server.HTTPServer(("127.0.0.1", 0), _RedirectingHandler)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server, port


def test_real_socket_redirect_is_rejected():
    server, port = _start_server()
    try:
        url = f"http://127.0.0.1:{port}/anything"  # Note: http:// is to avoid TLS; F2 tests the redirect behavior itself
        req = urllib.request.Request(url)
        with pytest.raises(EmgError) as excinfo:
            _OPENER.open(req, timeout=5)
        assert excinfo.value.code == "INSECURE_REDIRECT"
        assert "attacker.local" in excinfo.value.detail
    finally:
        server.shutdown()
        server.server_close()


def test_default_urllib_would_follow_redirect():
    """Control group: prove the default urllib behavior really is dangerous (without _NoRedirectHandler it follows).

    The local redirect server returns 302 → http://attacker.local/...; default
    urllib tries to connect to attacker.local; a DNS failure / refusal /
    aborted connection is all evidence that it "really followed". What we
    DO NOT want to see is "normal 200" — that would mean no follow, behavior
    matches our handler, and this control test is meaningless.
    """
    server, port = _start_server()
    try:
        url = f"http://127.0.0.1:{port}/anything"
        default_opener = urllib.request.build_opener()
        try:
            resp = default_opener.open(url, timeout=5)
            pytest.fail(f"expected redirect-follow failure; got 200 instead: {resp.status}")
        except EmgError:
            pytest.fail("EmgError from default opener — _NoRedirectHandler shouldn't be installed here")
        except Exception as e:
            # Any socket / DNS / connection error means a follow was actually attempted
            err = f"{type(e).__name__}: {e}"
            assert "attacker.local" in err.lower() or any(s in err for s in (
                "Name or service not known",
                "Temporary failure in name resolution",
                "Connection refused",
                "RemoteDisconnected",
                "Connection aborted",
            )), f"expected redirect-follow attempt, got unexpected: {err}"
    finally:
        server.shutdown()
        server.server_close()
