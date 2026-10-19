"""Loopback services shared by the interface tests; nothing leaves this machine."""

import json
import stat
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest


@pytest.fixture
def loopback_collector():
    """A fake collector that records each request's path and bearer token."""
    received_requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            received_requests.append(
                (self.path, self.headers["Authorization"], payload)
            )
            if self.path.endswith("/exchange"):
                status_code, response = (
                    200,
                    {"access_token": "collector-session-for-tests", "expires_in": 3600},
                )
            elif self.path == "/v1/runs":
                status_code, response = 201, {"registered": True}
            else:
                status_code, response = (
                    202,
                    {"accepted": len(payload["events"]), "duplicates": 0},
                )
            body = json.dumps(response).encode()
            self.send_response(status_code)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", received_requests
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


@pytest.fixture
def emitter_command_script(tmp_path):
    """Write an executable stand-in for the installed command."""

    def write(name: str, python_source: str) -> Path:
        path = tmp_path / "commands" / name
        path.parent.mkdir(exist_ok=True)
        path.write_text(f"#!{sys.executable}\n{python_source}")
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        return path

    return write


@pytest.fixture
def isolated_path(monkeypatch, tmp_path):
    """A PATH holding only one fresh directory."""
    directory = tmp_path / "path-directory"
    directory.mkdir()
    monkeypatch.setenv("PATH", str(directory))
    return directory
