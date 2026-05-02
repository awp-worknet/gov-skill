"""F2: HTTP 30x 重定向必须被拒绝 —— 防签名 header 跨域泄露。

跑一个本地 HTTPS server 不实际可行（要 cert），所以直接验证 handler 类
本身的行为：调 `_NoRedirectHandler.http_error_30X`，必须 raise EmgError
而不是返回新 Request。

外加一个真 socket 的 redirect smoke test（用 http://localhost 短路绕过
HTTPS enforcement，仅在 _NoRedirectHandler 装好的前提下测 opener 串起
来确实拦得住）。
"""

import http.server
import io
import os
import socket
import threading
import urllib.request

import pytest

from lib.govnet_lib import EmgError, _NoRedirectHandler, _OPENER


# --- 单元层：handler 类直接调用 ---------------------------------------------


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
    # 没 Location header 时 detail 里写 "?"
    assert "?" in excinfo.value.detail


# --- 集成层：起 mini HTTP server，验证 _OPENER 真的不 follow -----------------


class _RedirectingHandler(http.server.BaseHTTPRequestHandler):
    """收到 GET 就返回 302 → 一个完全不同的目标。"""
    def do_GET(self):
        self.send_response(302)
        self.send_header("Location", "http://attacker.local/never-follow-me")
        self.end_headers()

    def log_message(self, *args, **kwargs):
        pass  # 静默


def _start_server():
    """用 0 端口让 OS 分配一个空闲端口；返回 (server, port)。"""
    server = http.server.HTTPServer(("127.0.0.1", 0), _RedirectingHandler)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server, port


def test_real_socket_redirect_is_rejected():
    server, port = _start_server()
    try:
        url = f"http://127.0.0.1:{port}/anything"  # 注意 http:// 是为了避开 TLS；F2 测的是 redirect 行为本身
        req = urllib.request.Request(url)
        with pytest.raises(EmgError) as excinfo:
            _OPENER.open(req, timeout=5)
        assert excinfo.value.code == "INSECURE_REDIRECT"
        assert "attacker.local" in excinfo.value.detail
    finally:
        server.shutdown()
        server.server_close()


def test_default_urllib_would_follow_redirect():
    """对照组：证明默认 urllib 行为确实危险（不装 _NoRedirectHandler 就会 follow）。

    本地 redirect server 返回 302 → http://attacker.local/...，default urllib 会
    尝试连接 attacker.local；DNS 失败 / 拒接 / 连接断开都是"真的跟了"的证据。
    我们 NOT-want 看到的是 "正常 200" —— 那意味着没跟随、行为 = 我们的 handler
    （此时这个对照组测试没意义）。
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
            # 任何 socket / DNS / connection 错误都说明确实尝试 follow 了
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
