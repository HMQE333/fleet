"""K1: every browser step teaches Fleet something, whatever its outcome.

Expected shape: a migration adds `experience` (id, domain, goal, kind, status, reason, actions jsonb, cost_usd, seconds,
task_id, subtask_id, created_at); `fleet.experience.record(ctx, outcome_status, reason, actions, seconds)` is called by
both browser executors after a run (done, stuck, rejected, blocked, error alike), with the start URL's host as domain.
"""

import importlib

import pytest

from fleet import db, llm, worker
from fleet.executors import browser_fast
from fleet.fastbrowser import engine
from tests.conftest import finished, new_task, running_fleet, sub, subtasks, wait_for

TRACE = [{"kind": "fill", "label": "Search products", "value": "rpi 5"}, {"kind": "click", "label": "Search", "value": None}]


@pytest.fixture
async def fast_env(fake, monkeypatch):
    p = await db.pool()
    await p.execute("insert into profiles (adspower_id, name, enabled, general_use) values ('k1', 'Scratch', true, true)")
    monkeypatch.setitem(worker.EXECUTORS, "browser_fast", browser_fast.run)
    state = {"outcome": engine.Outcome("done", final={"url": "https://shop.test/r", "title": "R", "text": "399 zl"}, trace=TRACE)}
    try:
        browsers = importlib.import_module("fleet.browsers")

        async def fake_open(profile):
            class Opened:
                cdp_url = "ws://fake"
                kind = "local"
            return Opened()

        async def fake_close(profile):
            return None

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

    async def drive(cdp_url, ctx, hooks, decide_model, text_model, **kw):
        return state["outcome"]

    monkeypatch.setattr(browser_fast, "_drive", drive)

    async def complete(**kw):
        return "Cheapest: 399 zl"

    monkeypatch.setattr(llm, "complete", complete)
    return state


async def _rows():
    p = await db.pool()
    return [dict(r) for r in await p.fetch("select * from experience order by created_at")]


async def test_a_finished_step_is_recorded_with_its_actions_and_outcome(fake, fast_env):
    fake.plan.subtasks = [sub("b1", kind="browser_fast", url="https://shop.test/")]
    task = await new_task(prompt="find the cheapest raspberry pi 5")
    async with running_fleet():
        await wait_for(lambda: finished(task["id"]))
    rows = await _rows()
    assert len(rows) == 1, rows
    row = rows[0]
    assert row["domain"] == "shop.test" and row["kind"] == "browser_fast" and row["status"] == "done"
    assert row["actions"] == TRACE and row["goal"] == "instructions for b1"
    assert row["task_id"] == task["id"] and row["subtask_id"] == (await subtasks(task["id"]))["b1"]["id"]
    assert row["seconds"] is not None and row["cost_usd"] is not None


async def test_a_rejected_step_is_recorded_too_with_its_reason(fake, fast_env):
    fast_env["outcome"] = engine.Outcome("rejected", "The operator didn't approve “Send”", trace=[{"kind": "click", "label": "Compose", "value": None}])
    fake.plan.subtasks = [sub("b1", kind="browser_fast", url="https://mail.test/inbox")]
    task = await new_task()
    async with running_fleet():
        await wait_for(lambda: finished(task["id"]))
    rows = await _rows()
    assert len(rows) == 1 and rows[0]["status"] == "rejected" and "approve" in rows[0]["reason"]
    assert rows[0]["domain"] == "mail.test" and rows[0]["actions"] == [{"kind": "click", "label": "Compose", "value": None}]
