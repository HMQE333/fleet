"""browser_fast inside Fleet: hand-over to the full agent, no redo after approved actions, recorded paths."""

import pytest

from fleet import adspower, automations, db, llm, models, openrouter, worker
from fleet.executors import ExecResult
from fleet.executors import browser as full_browser
from fleet.executors import browser_fast
from fleet.fastbrowser import engine

from .conftest import finished, new_task, running_fleet, sub, subtasks, wait_for

TRACE = [{"kind": "fill", "label": "Search products", "value": "rpi 5"}, {"kind": "click", "label": "Search", "value": None}]


@pytest.fixture
async def fast_env(fake, monkeypatch):
    """Real browser_fast executor; AdsPower, the CDP drive and the full agent are stand-ins."""
    p = await db.pool()
    await p.execute("insert into profiles (adspower_id, name, enabled, general_use) values ('k1', 'Scratch', true, true)")
    monkeypatch.setitem(worker.EXECUTORS, "browser_fast", browser_fast.run)
    state = {"outcome": engine.Outcome("done", final={"url": "https://shop.test/r", "title": "R", "text": "399 zl"},
                                       trace=TRACE),
             "full_agent_runs": 0, "guides": []}

    async def start(adspower_id, headless=False):
        return adspower.OpenBrowser(adspower_id, "ws://fake", "1")

    async def stop(adspower_id):
        return None

    client = adspower.client()
    monkeypatch.setattr(client, "start", start)
    monkeypatch.setattr(client, "stop", stop)

    async def key():
        return "sk-or-test"

    monkeypatch.setattr(openrouter, "api_key", key)

    async def drive(cdp_url, ctx, hooks, decide_model, text_model, **kw):
        state["guides"].append(kw.get("guide", ctx.subtask.get("guide")))
        return state["outcome"]

    monkeypatch.setattr(browser_fast, "_drive", drive)

    async def full_agent(ctx):
        state["full_agent_runs"] += 1
        return ExecResult(result="full agent result", trace=[{"kind": "click", "label": "Publish", "value": None}])

    monkeypatch.setattr(full_browser, "run", full_agent)
    monkeypatch.setitem(worker.EXECUTORS, "browser", full_agent)

    async def complete(**kw):
        return "Cheapest: 399 zl"

    monkeypatch.setattr(llm, "complete", complete)
    return state


async def test_fast_run_answers_from_the_final_page_and_records_its_path(fake, fast_env):
    fake.plan.subtasks = [sub("b1", kind="browser_fast", url="https://shop.test/")]
    task = await new_task()
    async with running_fleet():
        t = await wait_for(lambda: finished(task["id"]))
    b1 = (await subtasks(task["id"]))["b1"]
    assert t["status"] == "done" and b1["kind"] == "browser_fast"
    assert b1["result"] == "Cheapest: 399 zl" and b1["trace"] == TRACE
    assert fast_env["full_agent_runs"] == 0


async def test_stuck_fast_run_hands_over_to_the_full_agent(fake, fast_env):
    fast_env["outcome"] = engine.Outcome("stuck", "Three actions in a row changed nothing")
    fake.plan.subtasks = [sub("b1", kind="browser_fast", url="https://shop.test/")]
    task = await new_task()
    async with running_fleet():
        await wait_for(lambda: finished(task["id"]))
    b1 = (await subtasks(task["id"]))["b1"]
    assert b1["status"] == "done" and b1["result"] == "full agent result"
    assert fast_env["full_agent_runs"] == 1


async def test_no_hand_over_after_an_approved_action(fake, fast_env):
    fast_env["outcome"] = engine.Outcome("stuck", "lost", acted=True)
    fake.plan.subtasks = [sub("b1", kind="browser_fast", url="https://shop.test/")]
    task = await new_task()
    async with running_fleet():
        await wait_for(lambda: finished(task["id"]))
    b1 = (await subtasks(task["id"]))["b1"]
    assert b1["status"] == "failed" and "repeat the action" in b1["error"]
    assert fast_env["full_agent_runs"] == 0


async def test_switching_fast_browser_off_uses_the_full_agent(fake, fast_env):
    await models.set_fast_browser(False)
    fake.plan.subtasks = [sub("b1", kind="browser_fast", url="https://shop.test/"), sub("b2", kind="browser_fast", url=None)]
    task = await new_task()
    async with running_fleet():
        await wait_for(lambda: finished(task["id"]))
    subs = await subtasks(task["id"])
    assert subs["b1"]["kind"] == subs["b2"]["kind"] == "browser"


async def test_learned_once_replayed_fast(fake, fast_env):
    """The full agent does it once; the automation replays that path with the fast browser."""
    fake.plan.subtasks = [sub("b1", kind="browser", url="https://shop.test/")]
    task = await new_task()
    async with running_fleet():
        await wait_for(lambda: finished(task["id"]))
    learned = (await subtasks(task["id"]))["b1"]
    assert learned["trace"] == [{"kind": "click", "label": "Publish", "value": None}]

    a = await automations.create_from_task(task["id"], "Publish", None, "UTC")
    run = await automations.start_run(a["id"])
    replay = (await subtasks(run["id"]))["b1"]
    assert replay["kind"] == "browser_fast" and replay["guide"] == learned["trace"]
    async with running_fleet():
        await wait_for(lambda: finished(run["id"]))
    assert fast_env["guides"][-1] == learned["trace"]  # the engine was given the recorded path


async def test_a_healed_replay_updates_the_saved_path_but_a_hand_over_or_a_refusal_never_does(fake, fast_env):
    """R3 through the worker: the engine reports a healed trace → the automation's stored step changes and the activity
    says so; a run handed to the full agent or stopped by the operator leaves the saved path alone."""
    fake.plan.subtasks = [sub("b1", kind="browser_fast", url="https://shop.test/")]
    task = await new_task()
    async with running_fleet():
        await wait_for(lambda: finished(task["id"]))
    a = await automations.create_from_task(task["id"], "Search", None, "UTC")
    assert a["steps"][0]["trace"] == TRACE
    p = await db.pool()

    async def stored():
        return (await p.fetchrow("select steps from automations where id = $1", a["id"]))["steps"][0]["trace"]

    async def path_updates(task_id):
        return await p.fetchval("select count(*) from events where task_id = $1 and message ilike 'path updated%'", task_id)

    healed = [TRACE[0], {"kind": "click", "label": "Find", "value": None}]
    fast_env["outcome"] = engine.Outcome("done", final={"url": "https://shop.test/r", "title": "R", "text": "ok"}, trace=healed, healed=1)
    run = await automations.start_run(a["id"])
    async with running_fleet():
        await wait_for(lambda: finished(run["id"]))
    assert (await subtasks(run["id"]))["b1"]["status"] == "done"
    assert await stored() == healed and await path_updates(run["id"]) == 1

    # Stuck: the full agent takes over and returns its own path; the saved one is not touched.
    fast_env["outcome"] = engine.Outcome("stuck", "Three actions in a row changed nothing",
                                         trace=[{"kind": "click", "label": "Menu", "value": None}], healed=1)
    run = await automations.start_run(a["id"])
    async with running_fleet():
        await wait_for(lambda: finished(run["id"]))
    assert (await subtasks(run["id"]))["b1"]["result"] == "full agent result"
    assert await stored() == healed and await path_updates(run["id"]) == 0

    # Rejected by the operator: the step fails (the run's review still writes an answer) and the saved path stays.
    fast_env["outcome"] = engine.Outcome("rejected", "The operator didn't approve “Buy”",
                                         trace=[{"kind": "click", "label": "Buy", "value": None}], healed=1)
    run = await automations.start_run(a["id"])
    async with running_fleet():
        await wait_for(lambda: finished(run["id"]))
    assert (await subtasks(run["id"]))["b1"]["status"] == "failed"
    assert await stored() == healed and await path_updates(run["id"]) == 0


async def test_real_fast_browser_through_fleet_with_an_approval(fake, browser_ws, site, monkeypatch, tmp_path):
    """Everything real except Jev's answers: Fleet worker → AdsPower stand-in (headless Edge) → engine → Inbox
    approval → screenshots, panel events, recorded path, side-effect mark, answer from the final page."""
    from fleet import hitl
    from fleet.config import settings
    from fleet.fastbrowser import decide, netguard

    from .conftest import scripted

    p = await db.pool()
    await p.execute("insert into profiles (adspower_id, name, enabled, general_use) values ('k1', 'Scratch', true, true)")
    monkeypatch.setitem(worker.EXECUTORS, "browser_fast", browser_fast.run)
    monkeypatch.setattr(settings(), "data_dir", tmp_path)
    monkeypatch.setattr(settings(), "close_browser_after_subtask", False)
    client = adspower.client()

    async def start(adspower_id, headless=False):
        return adspower.OpenBrowser(adspower_id, browser_ws, "0")

    monkeypatch.setattr(client, "start", start)

    async def key():
        return "sk-or-test"

    monkeypatch.setattr(openrouter, "api_key", key)

    async def local_is_fine(url):  # the test site is on 127.0.0.1; production refuses that
        return None

    monkeypatch.setattr(netguard, "check_url_resolved", local_is_fine)
    monkeypatch.setattr(decide, "choose", scripted([("fill", "Message"), ("click", "Send message"), "DONE"], []))

    async def field_text(goal, action, page, history, *, model, usage):
        return "Hello from Fleet"

    monkeypatch.setattr(decide, "field_text", field_text)
    seen_pages = []

    async def complete(*, user, **kw):
        seen_pages.append(user)
        return "The message was sent."

    monkeypatch.setattr(llm, "complete", complete)

    fake.plan.subtasks = [sub("b1", kind="browser_fast", url=f"{site}/compose.html")]
    task = await new_task()
    async with running_fleet():
        st = await wait_for(lambda: p.fetchrow("select * from subtasks where status = 'needs_human'"), timeout=30)
        assert st["human_request"]["kind"] == "approval" and "Send message" in st["human_request"]["message"]
        assert await hitl.respond(st["id"], "approve")
        t = await wait_for(lambda: finished(task["id"]), timeout=30)

    b1 = (await subtasks(task["id"]))["b1"]
    assert t["status"] == "done" and b1["status"] == "done", b1["error"]
    assert b1["result"] == "The message was sent." and "Message sent!" in seen_pages[0]
    assert b1["side_effects"] is True
    assert b1["trace"] == [{"kind": "fill", "label": "Message", "value": "Hello from Fleet"},
                           {"kind": "click", "label": "Send message", "value": None}]
    steps = await p.fetch("select data from events where subtask_id = $1 and type = 'step' order by id", b1["id"])
    assert len(steps) == 2 and all(s["data"]["engine"] == "fast" for s in steps)
    assert all((tmp_path / s["data"]["screenshot"]).exists() for s in steps)
