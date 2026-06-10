"""Stub OpenAI-compatible server fixture for contract tests.

Stands up a threaded HTTP server (stdlib only — no FastAPI/uvicorn) on
port 0 (OS-assigned) that:

  - POST /v1/chat/completions — records the request body and returns a
    canned chat completion response.
  - GET  /health              — returns {"status": "ok"} (used by startup
    probes in later tasks).

The fixture exposes:
  .url               — "http://127.0.0.1:<port>"
  .last_request_json — parsed JSON body of the last POST to /v1/chat/completions
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional

import pytest


# ---------------------------------------------------------------------------
# Canned response helper
# ---------------------------------------------------------------------------

def _make_completion(model: str) -> dict:
    """Return the minimal canned chat-completion JSON body."""
    return {
        "id": "stub",
        "object": "chat.completion",
        "created": 0,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": '{"ok": true}'},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 1},
    }


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------

class _StubHandler(BaseHTTPRequestHandler):
    """Minimal handler for /v1/chat/completions and /health."""

    # Suppress the default request-log lines so test output stays clean.
    def log_message(self, fmt, *args):  # noqa: D401
        pass

    def do_GET(self):
        if self.path == "/health" and getattr(self.server, "serve_health", True):
            body = b'{"status": "ok"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path != "/v1/chat/completions":
            self.send_response(404)
            self.end_headers()
            return

        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)
        try:
            parsed: dict = json.loads(raw)
        except json.JSONDecodeError:
            parsed = {}

        # Store on the server object so the fixture can read it back.
        self.server.last_request_json = parsed  # type: ignore[attr-defined]

        model = parsed.get("model", "stub-model")
        response_body = json.dumps(_make_completion(model)).encode()

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response_body)))
        self.end_headers()
        self.wfile.write(response_body)


# ---------------------------------------------------------------------------
# Fixture object
# ---------------------------------------------------------------------------

class StubOpenAIServer:
    """Thin wrapper around ThreadingHTTPServer that exposes url and last_request_json.

    ``serve_health=False`` makes GET /health return 404, modeling local
    OpenAI-compatible servers (vLLM at non-root paths, plain proxies) that
    do not expose llama-server's /health route.
    """

    def __init__(self, *, serve_health: bool = True) -> None:
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _StubHandler)
        self._server.last_request_json: Optional[dict] = None  # type: ignore[attr-defined]
        self._server.serve_health = serve_health  # type: ignore[attr-defined]
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="stub-openai-server",
            daemon=True,
        )
        self._thread.start()

    @property
    def url(self) -> str:
        host, port = self._server.server_address
        return f"http://{host}:{port}"

    @property
    def last_request_json(self) -> Optional[dict[str, Any]]:
        return self._server.last_request_json  # type: ignore[attr-defined]

    def shutdown(self) -> None:
        self._server.shutdown()
        self._thread.join(timeout=5)

    def close(self) -> None:
        """Fully stop the server: stop the accept loop, join the thread, and
        release the listening socket.  Use this when you need the port to be
        *refused* (not just no-longer-served) after teardown — e.g. to
        simulate an endpoint that has gone away mid-test."""
        self.shutdown()
        self._server.server_close()


# ---------------------------------------------------------------------------
# pytest fixture
# ---------------------------------------------------------------------------


@pytest.fixture()
def stub_openai_server():
    """Yield a running StubOpenAIServer; shut it down on teardown."""
    server = StubOpenAIServer()
    yield server
    server.close()
