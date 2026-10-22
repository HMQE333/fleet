"""Gate tests share Fleet's own fixtures (throwaway Postgres, headless browser, the scripted Jev stand-in) and add
a server for readiness/fixtures and the live-money guard."""

import functools
import http.server
import os
import pathlib
import threading

import pytest

from tests.conftest import (  # noqa: F401  (re-exported fixtures and hooks)
    browser_ws, clean_db, fake, pytest_collection_modifyitems, pytest_configure, pytest_sessionstart, site,
)

FIXTURES = pathlib.Path(__file__).resolve().parents[1] / "fixtures"
ROOT = pathlib.Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def rsite():
    """readiness/fixtures served on 127.0.0.1."""
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(FIXTURES))
    handler.log_message = lambda *a: None
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def script(entries: list, calls: list | None = None) -> callable:
    """A stand-in for Jev like tests.conftest.scripted, but it says what was on the page when a label is missing
    (the gate's reason then names the gap: "no 'Reveal secret' among the offered actions"), and a wanted label matches
    an offered one exactly or as a prefix ("Next page" fits "Next page »" whatever the page's charset did to it).
    `calls` collects the hint of every decision, so a test can count how often the model was asked."""
    queue = list(entries)

    async def choose(page, goal, history, *, model, usage, hint=None):
        if calls is not None:
            calls.append(hint)
        entry = queue.pop(0)
        if entry == "DONE":
            return {"choice": "DONE", "operation": "DONE", "probabilities": {}, "goal_done": 0.0, "stuck": 0.0, "confidence": 1.0}
        kind, label = entry
        if kind == "back":
            return {"choice": "back", "operation": "BACK", "probabilities": {"back": 0.9}, "goal_done": 0.0, "stuck": 0.0, "confidence": 0.9}
        offered = [a for a in page["actions"] if a["kind"] == kind]
        action = next((a for a in offered if a["label"] == label), None) or next((a for a in offered if a["label"].startswith(label)), None)
        if action is None:
            labels = ", ".join(repr(a["label"]) for a in page["actions"])[:600]
            raise AssertionError(f"no {kind} {label!r} among the offered actions on {page['url']}; offered: {labels}")
        return {"choice": action["id"], "operation": kind.upper(), "probabilities": {action["id"]: 0.9}, "goal_done": 0.0,
                "stuck": 0.0, "confidence": 0.9}

    return choose


def live_allowed() -> str | None:
    """None when a live gate may spend money, else the reason it can't (starts with the level, see READINESS.md)."""
    if os.environ.get("FLEET_READINESS_LIVE", "1") == "0":
        return "L1: live gates switched off (--no-live)"
    if not (os.environ.get("FLEET_READINESS_OPENROUTER_KEY") or os.environ.get("FLEET_READINESS_ANTHROPIC_KEY")):
        return "L1: no API key in .env or the environment (the readiness runner passes them as FLEET_READINESS_*_KEY)"
    return None


@pytest.fixture
def live():
    """The ledger for a live gate, with the real keys in place for this test only (tests/conftest.py blanks them).

    The keys go through a MonkeyPatch of this fixture's own, not the shared `monkeypatch` fixture: a test that wants
    the real thing back calls `monkeypatch.undo()` (C5 does, for the real planner), and that would otherwise take the
    keys down together with the stand-ins it meant to drop."""
    reason = live_allowed()
    if reason:
        pytest.skip(reason)
    from fleet import openrouter
    from readiness.spend import Ledger

    key = os.environ.get("FLEET_READINESS_OPENROUTER_KEY", "")
    mp = pytest.MonkeyPatch()
    mp.setenv("OPENROUTER_API_KEY", key)
    mp.setenv("ANTHROPIC_API_KEY", os.environ.get("FLEET_READINESS_ANTHROPIC_KEY", ""))

    async def api_key():  # the key comes straight from the environment, so these tests need no vault and no database
        return key

    mp.setattr(openrouter, "api_key", api_key)
    mp.setattr(openrouter, "_client", None)  # the cached httpx client belongs to the previous test's event loop
    try:
        yield Ledger(ROOT / "readiness" / "spend.jsonl", float(os.environ.get("FLEET_READINESS_BUDGET_USD", "0.25")))
    finally:
        mp.undo()
