"""Shared fixtures: a hermetic static server for the hostile fixture app,
plus a session-scoped Chromium browser so tests don't relaunch Playwright."""

from __future__ import annotations

from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import pytest

FIXTURE_APP = Path(__file__).parent / "fixture_app"

# Credentials accepted by the fixture app's client-side login form.
FIXTURE_USER = "cook@example.test"
FIXTURE_PASSWORD = "sourdough"


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args: object) -> None:  # noqa: D102 — silence request logs
        pass


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
