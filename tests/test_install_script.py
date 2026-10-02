"""Dry run of deploy/install.sh (DP-6) with a stub docker and a local download server."""

import functools
import http.server
import os
import shutil
import subprocess
import threading
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "deploy" / "install.sh"

pytestmark = pytest.mark.skipif(shutil.which("sh") is None, reason="needs sh")

ENV_EXAMPLE = """TZ=Europe/Amsterdam
TELLYBOX_WEB_PORT=8080
TELLYBOX_CAST_API_PORT=8081
# TELLYBOX_TAG=latest
"""

STUB_DOCKER = """#!/bin/sh
echo "$*" >> "$STUB_LOG"
case "$1 $2" in
  "info --format") echo "${STUB_OS:-Ubuntu 24.04 LTS}" ;;
  "compose logs") echo "tellybox  | admin setup code: ABCD-2345" ;;
esac
exit 0
"""


@pytest.fixture
def server(tmp_path):
    root = tmp_path / "release"
    root.mkdir()
    (root / "docker-compose.yml").write_text("services:\n  tellybox: {}\n")
    (root / "env.example").write_text(ENV_EXAMPLE)
    handler = functools.partial(
        http.server.SimpleHTTPRequestHandler, directory=str(root)
    )
    handler.log_message = lambda *a, **k: None
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


def run_install(tmp_path, server, extra_env=None, args=("--yes",)):
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    docker = bindir / "docker"
    docker.write_text(STUB_DOCKER)
    docker.chmod(0o755)
    log = tmp_path / "docker.log"
    log.touch()
    target = tmp_path / "install"
    env = {
        **os.environ,
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "STUB_LOG": str(log),
        "TELLYBOX_INSTALL_BASE_URL": server,
        "TELLYBOX_INSTALL_SKIP_HEALTH": "1",
        "TELLYBOX_DIR": str(target),
        "TZ": "America/New_York",
        "TELLYBOX_WEB_PORT": "18095",
        **(extra_env or {}),
    }
    proc = subprocess.run(
        ["sh", str(SCRIPT), *args],
        env=env,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        timeout=60,
    )
    return proc, target, log.read_text()


def test_fresh_install(tmp_path, server):
    proc, target, log = run_install(tmp_path, server)
    assert proc.returncode == 0, proc.stderr
    env = (target / ".env").read_text()
    assert "TZ=America/New_York\n" in env
    assert "TELLYBOX_WEB_PORT=18095\n" in env
    assert "TELLYBOX_CAST_API_PORT=8081" in env
    assert (target / "docker-compose.yml").exists()
    assert "compose up -d" in log
    assert "ABCD-2345" in proc.stdout
    assert ":18095/admin/setup" in proc.stdout


def test_pinned_version_sets_tag(tmp_path, server):
    proc, target, _ = run_install(tmp_path, server, {"TELLYBOX_VERSION": "v0.1.0"})
    assert proc.returncode == 0, proc.stderr
    assert "TELLYBOX_TAG=0.1.0\n" in (target / ".env").read_text()


def test_existing_env_is_kept(tmp_path, server):
    target = tmp_path / "install"
    target.mkdir()
    (target / ".env").write_text("TZ=Asia/Tokyo\nTELLYBOX_WEB_PORT=9999\n")
    proc, _, log = run_install(tmp_path, server)
    assert proc.returncode == 0, proc.stderr
    assert (target / ".env").read_text() == "TZ=Asia/Tokyo\nTELLYBOX_WEB_PORT=9999\n"
    assert (target / "docker-compose.yml").exists()
    assert "compose pull" in log and "compose up -d" in log
    assert ":9999/" in proc.stdout


def test_refuses_docker_desktop(tmp_path, server):
    proc, target, log = run_install(tmp_path, server, {"STUB_OS": "Docker Desktop"})
    assert proc.returncode == 1
    assert "Docker Desktop" in proc.stderr
    assert "compose up" not in log
    assert not target.exists()


def test_syntax_with_dash():
    dash = shutil.which("dash")
    if dash is None:
        pytest.skip("no dash")
    subprocess.run([dash, "-n", str(SCRIPT)], check=True)
