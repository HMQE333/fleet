"""K2: the experience base answers two questions: "how did a goal like this go on this site?" and "what do we know about
this site?".

Expected shape: `fleet.experience.best_path(domain, goal) -> list[dict] | None` (the actions of the latest *successful*
record on that domain whose goal is similar: shared words after stop-word removal, >= 0.6 of the shorter goal; a different
goal returns None); `fleet.experience.site_notes(domain) -> str` (empty when nothing is known; else counts of successes
and failures, the last failure's reason, whether a login wall was met), never longer than 600 characters.
"""

import importlib
from datetime import UTC, datetime, timedelta

import pytest

from fleet import db


def _exp():
    try:
        return importlib.import_module("fleet.experience")
    except ImportError as e:
        pytest.fail(f"fleet.experience is missing: {e}")


async def _add(domain, goal, status, actions, reason="", when=None):
    p = await db.pool()
    await p.execute(
        "insert into experience (domain, goal, kind, status, reason, actions, cost_usd, seconds, created_at) "
        "values ($1, $2, 'browser_fast', $3, $4, $5, 0.01, 3, $6)",
        domain, goal, status, reason, actions, when or datetime.now(UTC))


async def test_best_path_matches_a_similar_goal_and_prefers_the_latest_success():
    exp = _exp()
    old = [{"kind": "click", "label": "Old", "value": None}]
    new = [{"kind": "fill", "label": "Search products", "value": "rpi 5"}, {"kind": "click", "label": "Search", "value": None}]
    await _add("shop.test", "search the shop for raspberry pi 5 and open the first result", "done", old, when=datetime.now(UTC) - timedelta(days=2))
    await _add("shop.test", "find raspberry pi 5 in the shop search and open the first result", "done", new)
    await _add("shop.test", "find raspberry pi 5 in the shop search", "stuck", [{"kind": "click", "label": "Broken", "value": None}], "Three actions changed nothing")
    assert await exp.best_path("shop.test", "search for raspberry pi 5 and open the first result") == new
    assert await exp.best_path("shop.test", "change my delivery address") is None
    assert await exp.best_path("other.test", "search for raspberry pi 5") is None


async def test_site_notes_sum_up_what_happened_there():
    exp = _exp()
    assert await exp.site_notes("nothing.test") == ""
    await _add("news.test", "read the front page", "done", [])
    await _add("news.test", "post a comment", "stopped", [], "This page asks for a login")
    await _add("news.test", "post a comment", "rejected", [], "The operator didn't approve “Post”")
    notes = await exp.site_notes("news.test")
    assert "1" in notes and "2" in notes  # one success, two that didn't finish
    assert "login" in notes.lower() and "approve" in notes.lower()
    assert len(notes) <= 600
