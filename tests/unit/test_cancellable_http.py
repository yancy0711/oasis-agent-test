"""Real loopback HTTP checks for cancellation and bounded response reads."""
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Event, Thread
from time import monotonic
from types import SimpleNamespace

import httpx
import pytest

from oasis.core import http
from oasis.core.cancellation import CancellationToken, RunCancelled


@pytest.fixture
def http_server(monkeypatch):
    # Never send loopback test traffic through a developer's HTTP proxy.
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    state = SimpleNamespace(started=Event(), released=Event(), stop=Event(), paths=[])

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            state.paths.append(self.path)
            if self.path == "/stall":
                self.send_response(200)
                self.send_header("Content-Length", "12")
                self.end_headers()
                self.wfile.write(b"{")
                self.wfile.flush()
                state.started.set()
                # The client is waiting for the body. Observe that cancellation
                # actually closes its socket, not just the calling Future.
                self.connection.settimeout(4)
                if self.connection.recv(1) == b"":
                    state.released.set()
                return
            if self.path == "/drip":
                self.send_response(200)
                self.send_header("Content-Length", "200")
                self.end_headers()
                try:
                    for _ in range(200):
                        self.wfile.write(b" ")
                        self.wfile.flush()
                        if state.stop.wait(0.02):
                            break
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass
                return
            body = b'{"ok": true}'
            self.send_response(503 if self.path == "/unavailable" else 200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    state.url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        yield state
    finally:
        state.stop.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_cancel_interrupts_body_read_closes_socket_and_allows_next_request(http_server):
    token = CancellationToken()
    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(http.cancellable_request, "GET", http_server.url + "/stall",
                                  check=token.check, timeout=(0.5, 3))
        assert http_server.started.wait(2), "The request never reached the local server"
        started = monotonic()
        token.cancel()
        with pytest.raises(RunCancelled):
            pending.result(timeout=2)
        assert monotonic() - started < 1
        assert http_server.released.wait(1), "Cancellation left the HTTP socket open"
    response = http.cancellable_request("GET", http_server.url + "/ok", timeout=(0.5, 1))
    assert response.status_code == 200 and response.json() == {"ok": True}
    assert http_server.paths == ["/stall", "/ok"]


def test_pre_cancel_does_not_even_construct_http_client(monkeypatch):
    token = CancellationToken()
    token.cancel()
    monkeypatch.setattr(http.httpx, "AsyncClient", lambda **kwargs: pytest.fail("Cancelled request created a client"))
    with pytest.raises(RunCancelled):
        http.cancellable_request("POST", "https://unused.invalid", check=token.check)


def test_wall_deadline_stops_response_that_keeps_delivering_bytes(http_server):
    started = monotonic()
    # Each body chunk arrives every 20 ms, well below the 700 ms read timeout.
    # Only the wall deadline can stop this stream at about 900 ms.
    with pytest.raises(httpx.ReadTimeout, match="wall deadline"):
        http.cancellable_request("GET", http_server.url + "/drip", timeout=(0.2, 0.7))
    assert monotonic() - started < 2


def test_http_status_and_body_remain_available_to_provider_error_handler(http_server):
    response = http.cancellable_request("GET", http_server.url + "/unavailable", timeout=(0.5, 1))
    assert response.status_code == 503
    assert response.json() == {"ok": True}
    with pytest.raises(httpx.HTTPStatusError):
        response.raise_for_status()
