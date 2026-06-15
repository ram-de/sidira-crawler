"""Shared fixtures: a hermetic static server for the hostile fixture app,
plus a session-scoped Chromium browser so tests don't relaunch Playwright."""

from __future__ import annotations

import time
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import pytest

FIXTURE_APP = Path(__file__).parent / "fixture_app"

# Credentials accepted by the fixture app's client-side login form.
FIXTURE_USER = "cook@example.test"
FIXTURE_PASSWORD = "sourdough"

# How long GET /slow sleeps before answering — long enough that a premature
# settle (one that ignores in-flight requests) reliably returns first.
SLOW_ENDPOINT_SECONDS = 1.5

# GET /longpoll never answers within a test's lifetime: it models the
# never-idle real-app case (long-poll / SSE / heartbeat) where a request stays
# in-flight forever, so the page never reaches networkidle and the walker's
# pending set is never empty. Outlasts _settle's hard cap by a wide margin.
LONGPOLL_ENDPOINT_SECONDS = 30.0

# GET /chunk answers after a short delay: the slow-render fixture fetches a
# sequence of these and appends each to the DOM on resolve, so the document
# keeps growing for ~1s with a request in-flight the whole time. _settle must
# not cut this short (recall loss) — its stable-despite-pending escape has to
# outlast a single chunk's latency.
CHUNK_ENDPOINT_SECONDS = 0.12


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args: object) -> None:  # noqa: D102 — silence request logs
        pass

    def do_GET(self) -> None:  # noqa: D102 — adds slow / never-idle API endpoints
        # Match on the path without its query string and compare exactly, so an
        # API route never shadows a static file that merely shares its prefix
        # (e.g. /slow must not swallow a navigation to /slowrender.html).
        route = self.path.split("?", 1)[0]
        if route == "/slow":
            time.sleep(SLOW_ENDPOINT_SECONDS)
            self.send_response(204)
            self.end_headers()
            return
        if route == "/longpoll":
            time.sleep(LONGPOLL_ENDPOINT_SECONDS)
            self.send_response(204)
            self.end_headers()
            return
        if route == "/chunk":
            time.sleep(CHUNK_ENDPOINT_SECONDS)
            body = b"ingredient"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        super().do_GET()


@pytest.fixture(scope="session")
def fixture_server() -> str:
    handler = partial(_QuietHandler, directory=str(FIXTURE_APP))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()


@pytest.fixture(scope="session")
def browser():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        launched = playwright.chromium.launch(headless=True)
        try:
            yield launched
        finally:
            launched.close()
