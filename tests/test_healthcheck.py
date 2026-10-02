import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from tellybox import healthcheck

WEB = ["python", "-m", "tellybox.web"]
CAST = ["python", "-m", "tellybox.cast"]


def test_target_urls():
    assert healthcheck.target_url(WEB, {}) == "http://127.0.0.1:8080/healthz"
    all_in_one = healthcheck.target_url(["python", "-m", "tellybox"], {"TELLYBOX_WEB_PORT": "9"})
    assert all_in_one == "http://127.0.0.1:9/healthz"
    env = {"TELLYBOX_CAST_API_HOST": "10.0.0.1", "TELLYBOX_CAST_API_PORT": "7"}
    assert healthcheck.target_url(CAST, env) == "http://10.0.0.1:7/state"
    assert healthcheck.target_url(CAST, {}) == "http://127.0.0.1:8081/state"
    assert healthcheck.target_url(["python", "-m", "tellybox.worker"], {}) is None
    assert healthcheck.target_url([], {}) is None


def test_worker_is_healthy_without_network():
    def boom(*a, **k):
        raise AssertionError("no request expected")

    assert healthcheck.check(["python", "-m", "tellybox.worker"], {}, urlopen=boom)


def test_read_cmdline(tmp_path):
    f = tmp_path / "cmdline"
    f.write_bytes(b"python\0-m\0tellybox.web\0")
    assert healthcheck.read_cmdline(str(f)) == WEB
    assert healthcheck.read_cmdline(str(tmp_path / "missing")) == []


@pytest.fixture
def server():
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200 if self.path == "/healthz" else 500)
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address[1]
    srv.shutdown()


def test_web_healthy_and_unhealthy(server):
    assert healthcheck.check(WEB, {"TELLYBOX_WEB_PORT": str(server)})
    # /state answers 500 on this server
    assert not healthcheck.check(CAST, {"TELLYBOX_CAST_API_PORT": str(server)})


def test_connection_refused_is_unhealthy():
    assert not healthcheck.check(WEB, {"TELLYBOX_WEB_PORT": "1"})
