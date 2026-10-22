"""Self-healing replay (R3): a recorded path that no longer fits the page costs one Jev decision, the engine says how many
recorded steps it replaced, and the saved automation step learns the path that worked today."""

import functools
import http.server
import pathlib
import threading

import pytest

from fleet import automations, db, llm
from fleet.fastbrowser import decide, engine
from fleet.fastbrowser.cdp import CDP

from .conftest import scripted

READINESS_FIXTURES = pathlib.Path(__file__).resolve().parent.parent / "readiness" / "fixtures"
# Recorded on tests/fixtures/fast/shop.html, where the button says "Search"; readiness/fixtures/shop_v2.html renamed it "Find".
GUIDE = [{"kind": "fill", "label": "Search products", "value": "raspberry pi 5"},
         {"kind": "click", "label": "Search", "value": None}]


@pytest.fixture(scope="module")
def redesigned_site():
    """readiness/fixtures served on 127.0.0.1 (the redesigned shop lives next to the gates)."""
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(READINESS_FIXTURES))
    handler.log_message = lambda *a: None
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


async def _replay(browser_ws, url, script, monkeypatch):
    calls = []
    monkeypatch.setattr(decide, "choose", scripted(list(script), calls))

    async def field_text(goal, action, page, history, *, model, usage):
        raise AssertionError("a recorded or healed fill types the recorded value, it never asks the text model")

    monkeypatch.setattr(decide, "field_text", field_text)
    async with CDP(browser_ws) as cdp:
        out = await engine.run(cdp, url=url, goal="search for raspberry pi 5", hooks=engine.Hooks(), usage=llm.Usage(),
                               decide_model="m", text_model="m", guide=GUIDE, allow_private=True)
    return out, calls


@pytest.mark.no_db
def test_a_replacement_is_the_same_kind_of_action_under_another_label():
    recorded = {"kind": "click", "label": "Search", "value": None}
    assert engine.replaces(recorded, {"kind": "click", "label": "Find"})
    assert engine.replaces(recorded, {"kind": "click", "label": "  search "}) is False  # the recorded step itself
    assert engine.replaces(recorded, {"kind": "fill", "label": "Find"}) is False  # an extra step, not a stand-in
    assert engine.replaces({"kind": "back", "label": None}, {"kind": "back", "label": "Back"}) is False
    assert engine.replaces(None, {"kind": "click", "label": "Find"}) is False


@pytest.mark.no_db
async def test_one_renamed_button_costs_one_decision_and_counts_as_healed(browser_ws, redesigned_site, monkeypatch):
    out, calls = await _replay(browser_ws, f"{redesigned_site}/shop_v2.html", [("click", "Find"), "DONE"], monkeypatch)
    assert out.status == "done", out.reason
    assert calls == ['click “Search”', None]  # one decision, with the recorded step as the hint, then the final DONE
    assert out.healed == 1
    assert out.trace == [{"kind": "fill", "label": "Search products", "value": "raspberry pi 5"},
                         {"kind": "click", "label": "Find", "value": None}]
    assert [(s["guided"], s["healed"]) for s in out.steps] == [(True, False), (False, True)]


@pytest.mark.no_db
async def test_a_path_that_still_fits_heals_nothing(browser_ws, site, monkeypatch):
    out, calls = await _replay(browser_ws, f"{site}/shop.html", ["DONE"], monkeypatch)
    assert out.status == "done", out.reason
    assert out.healed == 0 and calls == [None]
    assert out.trace == GUIDE and not any(s["healed"] for s in out.steps)


async def test_update_trace_changes_only_that_steps_path():
    p = await db.pool()
    steps = [{"key": "b1", "title": "Search", "kind": "browser_fast", "instructions": "search", "start_url": "https://shop.test/",
              "depends_on": [], "model_tier": "fast", "profile_id": None, "trace": GUIDE, "recipe": None},
             {"key": "f1", "title": "Fetch", "kind": "fetch", "instructions": "read", "start_url": "https://shop.test/x",
              "depends_on": ["b1"], "model_tier": "fast", "profile_id": None, "trace": None, "recipe": None}]
    auto_id = await p.fetchval("insert into automations (name, prompt, steps) values ('Shop', 'search', $1) returning id", steps)
    new = [GUIDE[0], {"kind": "click", "label": "Find", "value": None}]

    assert await automations.update_trace(auto_id, "b1", new) is True
    stored = (await p.fetchrow("select steps from automations where id = $1", auto_id))["steps"]
    assert stored[0] == {**steps[0], "trace": new} and stored[1] == steps[1]
    assert await automations.update_trace(auto_id, "b1", new) is False  # already this path
    assert await automations.update_trace(auto_id, "f1", new) is False  # not a browser step
    assert await automations.update_trace(auto_id, "nope", new) is False
    assert (await p.fetchrow("select steps from automations where id = $1", auto_id))["steps"] == stored
