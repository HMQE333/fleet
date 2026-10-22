"""R3: a recorded path heals itself. One label changed on the site → one model decision, and the saved path is updated
so the next replay needs none.

Expected shape: `engine.run` already follows a guide and asks Jev where it doesn't fit. What is missing today:
- the trace of a replay that drifted is written back to the saved automation step (`automations.update_trace(...)`
  or equivalent, called by the browser_fast executor when `ctx.task['automation_id']` is set and the guide changed);
- `engine.Outcome.healed` (or an equivalent field) says how many recorded steps were replaced.
"""

import pytest

from fleet import llm
from fleet.fastbrowser import decide, engine
from fleet.fastbrowser.cdp import CDP
from readiness.gates.conftest import script

@pytest.mark.no_db
async def test_one_changed_label_costs_one_decision_and_updates_the_path(browser_ws, rsite, monkeypatch):
    # The path was recorded on shop.html ("Search"); shop_v2.html renamed the button to "Find".
    guide = [{"kind": "fill", "label": "Search products", "value": "raspberry pi 5"},
             {"kind": "click", "label": "Search", "value": None}]
    calls = []
    monkeypatch.setattr(decide, "choose", script([("click", "Find"), "DONE"], calls))

    async def field_text(goal, action, page, history, *, model, usage):
        return "raspberry pi 5"

    monkeypatch.setattr(decide, "field_text", field_text)
    async with CDP(browser_ws) as cdp:
        out = await engine.run(cdp, url=f"{rsite}/shop_v2.html", goal="search for raspberry pi 5", hooks=engine.Hooks(),
                               usage=llm.Usage(), decide_model="m", text_model="m", guide=guide, allow_private=True)
    assert out.status == "done", out.reason
    assert len(calls) == 2, f"expected one decision for the drifted step plus the final DONE, got {len(calls)}"
    assert out.trace == [{"kind": "fill", "label": "Search products", "value": "raspberry pi 5"},
                         {"kind": "click", "label": "Find", "value": None}]
    assert getattr(out, "healed", None) == 1, "Outcome.healed should count the recorded steps that were replaced"


async def test_a_drifted_replay_writes_the_new_path_back_to_the_automation(fake, monkeypatch):
    """Through Fleet: a saved browser step replays, the engine reports a healed trace, the automation's stored step changes."""
    from fleet import automations, db, worker
    from fleet.executors import browser_fast
    from tests.conftest import finished, new_task, running_fleet, sub, subtasks, wait_for

    p = await db.pool()
    await p.execute("insert into profiles (adspower_id, name, enabled, general_use) values ('k1', 'Scratch', true, true)")
    monkeypatch.setitem(worker.EXECUTORS, "browser_fast", browser_fast.run)
    old = [{"kind": "click", "label": "Search", "value": None}]
    new = [{"kind": "click", "label": "Find", "value": None}]

    async def fake_open(profile):
        class Opened:
            cdp_url = "ws://fake"
            kind = "local"
        return Opened()

    async def fake_close(profile):
        return None

    import importlib

    try:
        browsers = importlib.import_module("fleet.browsers")
        monkeypatch.setattr(browsers, "open", fake_open)
        monkeypatch.setattr(browsers, "close", fake_close)
    except ImportError:
        from fleet import adspower

        async def start(adspower_id, headless=False):
            return adspower.OpenBrowser(adspower_id, "ws://fake", "1")

        async def stop(adspower_id):
            return None

        monkeypatch.setattr(adspower.client(), "start", start)
        monkeypatch.setattr(adspower.client(), "stop", stop)
    from fleet import openrouter

    async def key():
        return "sk-or-test"

    monkeypatch.setattr(openrouter, "api_key", key)
    state = {"trace": old}

    async def drive(cdp_url, ctx, hooks, decide_model, text_model, **kw):
        out = engine.Outcome("done", final={"url": "https://shop.test/r", "title": "R", "text": "ok"}, trace=state["trace"])
        out.healed = 1 if state["trace"] == new else 0
        return out

    monkeypatch.setattr(browser_fast, "_drive", drive)

    async def complete(**kw):
        return "answer"

    monkeypatch.setattr(llm, "complete", complete)

    fake.plan.subtasks = [sub("b1", kind="browser_fast", url="https://shop.test/")]
    task = await new_task()
    async with running_fleet():
        await wait_for(lambda: finished(task["id"]))
    a = await automations.create_from_task(task["id"], "Search", None, "UTC")
    assert a["steps"][0]["trace"] == old

    state["trace"] = new
    run = await automations.start_run(a["id"])
    async with running_fleet():
        await wait_for(lambda: finished(run["id"]))
    assert (await subtasks(run["id"]))["b1"]["status"] == "done"
    stored = await p.fetchrow("select steps from automations where id = $1", a["id"])
    assert stored["steps"][0]["trace"] == new, "the automation still holds the old path after a healed replay"
