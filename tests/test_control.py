"""Operator control over running work: pause a task, take over a running browser step.

The rules under test (ROADMAP S4): a paused task starts nothing new but never kills what is running; resuming
continues; taking over pauses a browser step at its next step (nothing is clicked while the operator is in the
window), and handing control back continues from whatever the page shows; only the operator's own request does it.
"""

import asyncio

import httpx
import pytest

from fleet import db, hitl, worker
from fleet.api import create_app
from fleet.fastbrowser import decide

from .conftest import finished, new_task, running_fleet, scripted, sub, subtasks, wait_for
from .test_site_logins import env  # noqa: F401  (the real browser_fast worker over a headless Edge)

PANEL = {"X-Fleet-Client": "panel"}


@pytest.fixture
async def client():
    app = create_app(run_background=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8710") as c:
        yield c


async def _queued(kind: str, *, paused: bool = False, take_over: bool = False) -> dict:
    """A running task with one queued step, written straight to the database."""
    p = await db.pool()
    task = await p.fetchrow("insert into tasks (prompt, status, paused) values ('x', 'running', $1) returning id", paused)
    step = await p.fetchrow(
        "insert into subtasks (task_id, round, key, title, kind, instructions, status, take_over) "
        "values ($1, 1, 's1', 't', $2, 'x', 'queued', $3) returning id", task["id"], kind, take_over)
    return {"task": task["id"], "step": step["id"]}


async def _profile() -> None:
    p = await db.pool()
    await p.execute("insert into profiles (adspower_id, name, enabled, general_use) values ('k1', 'Scratch', true, true)")


# -- pause ------------------------------------------------------------------------------------------------------


async def test_a_paused_task_claims_no_new_steps_and_a_resumed_one_does():
    await _profile()
    web = await _queued("fetch", paused=True)
    browser = await _queued("browser_fast", paused=True)
    w = worker.Worker("test")
    assert await w._claim_web() is None and await w._claim_browser() is None  # paused: nothing starts
    p = await db.pool()
    await p.execute("update tasks set paused = false")
    claimed_web = await w._claim_web()
    claimed_browser = await w._claim_browser()
    assert claimed_web["id"] == web["step"] and claimed_browser[0]["id"] == browser["step"]


async def test_a_paused_task_finishes_after_it_is_resumed(fake, client):
    fake.plan.subtasks = [sub("a"), sub("b")]
    task = await new_task()
    p = await db.pool()
    await p.execute("update tasks set paused = true where id = $1", task["id"])
    async with running_fleet():
        await wait_for(lambda: p.fetchval("select count(*) = 2 from subtasks where task_id = $1", task["id"]))
        await asyncio.sleep(2.5)  # long enough for several claim rounds
        assert {s["status"] for s in (await subtasks(task["id"])).values()} <= {"pending", "queued"}
        assert (await client.post(f"/api/tasks/{task['id']}/resume", headers=PANEL)).status_code == 200
        t = await wait_for(lambda: finished(task["id"]), timeout=30)
    assert t["status"] == "done" and all(s["status"] == "done" for s in (await subtasks(task["id"])).values())
    assert await p.fetchval("select count(*) from events where type = 'task.resumed'") == 1


async def test_pause_and_resume_api_rules(client):
    p = await db.pool()
    running = (await _queued("fetch"))["task"]
    done = await p.fetchval("insert into tasks (prompt, status) values ('x', 'done') returning id")
    assert (await client.post(f"/api/tasks/{running}/pause")).status_code == 403  # CSRF guard
    assert (await client.post(f"/api/tasks/{done}/pause", headers=PANEL)).status_code == 409  # only a running task
    assert (await client.post(f"/api/tasks/{running}/resume", headers=PANEL)).status_code == 409  # not paused
    assert (await client.post(f"/api/tasks/{running}/pause", headers=PANEL)).status_code == 200
    assert await p.fetchval("select paused from tasks where id = $1", running) is True
    listed = {t["id"]: t for t in (await client.get("/api/tasks")).json()}
    assert listed[str(running)]["paused"] is True and (await client.get(f"/api/tasks/{running}")).json()["task"]["paused"]
    assert await p.fetchval("select count(*) from events where type = 'task.paused'") == 1
    assert (await client.post(f"/api/tasks/{running}/resume", headers=PANEL)).status_code == 200
    assert await p.fetchval("select paused from tasks where id = $1", running) is False


# -- take over: the API --------------------------------------------------------------------------------------------


async def test_only_a_running_browser_step_can_be_taken_over(client):
    p = await db.pool()
    steps = {kind: (await _queued(kind))["step"] for kind in ("browser_fast", "browser", "fetch")}
    for step in steps.values():
        await p.execute("update subtasks set status = 'running' where id = $1", step)
    queued = (await _queued("browser_fast"))["step"]
    assert (await client.post(f"/api/subtasks/{steps['browser_fast']}/take-over")).status_code == 403  # CSRF guard
    assert (await client.post(f"/api/subtasks/{steps['fetch']}/take-over", headers=PANEL)).status_code == 409
    assert (await client.post(f"/api/subtasks/{queued}/take-over", headers=PANEL)).status_code == 409  # not running yet
    assert (await client.post("/api/subtasks/00000000-0000-0000-0000-000000000000/take-over", headers=PANEL)).status_code == 409
    for kind in ("browser_fast", "browser"):
        assert (await client.post(f"/api/subtasks/{steps[kind]}/take-over", headers=PANEL)).status_code == 200
        assert await p.fetchval("select take_over from subtasks where id = $1", steps[kind]) is True
    assert await p.fetchval("select count(*) from events where type = 'subtask.take_over'") == 2


async def test_the_request_is_read_once_and_a_new_claim_forgets_an_old_one():
    await _profile()
    step = await _queued("browser_fast", take_over=True)
    claimed, _ = await worker.Worker("test")._claim_browser()
    assert claimed["take_over"] is False  # an old request must not pause a retry that starts fresh
    p = await db.pool()
    await p.execute("update subtasks set take_over = true where id = $1", step["step"])
    assert await hitl.take_over_requested(step["step"]) is True
    assert await hitl.take_over_requested(step["step"]) is False  # one pause per request
    assert "browser window for profile “Scratch”" in hitl.take_over_message("Scratch")
    assert "browser window" in hitl.take_over_message(None)


# -- take over: the real fast browser worker --------------------------------------------------------------------------


@pytest.fixture
async def mid_run(env, client, monkeypatch):  # noqa: F811
    """Jev fills the message box; the operator clicks "Take over" while the worker is working."""
    inner = scripted([("fill", "Message"), "DONE"], [])
    calls = []

    async def choose(page, goal, history, *, model, usage, hint=None):
        calls.append(1)
        if len(calls) == 1:
            sid = await env.p.fetchval("select id from subtasks where status = 'running'")
            assert (await client.post(f"/api/subtasks/{sid}/take-over", headers=PANEL)).status_code == 200
        return await inner(page, goal, history, model=model, usage=usage, hint=hint)

    async def field_text(goal, action, page, history, *, model, usage):
        return "Hello"

    monkeypatch.setattr(decide, "choose", choose)
    monkeypatch.setattr(decide, "field_text", field_text)
    return env


async def test_taking_over_pauses_the_worker_until_control_is_given_back(mid_run):
    task = await mid_run.run("compose.html")
    async with running_fleet():
        card = await mid_run.card()
        assert card["human_request"]["kind"] == "takeover" and "Scratch" in card["human_request"]["message"]
        assert card["status"] == "needs_human"
        assert await mid_run.p.fetchval("select take_over from subtasks where id = $1", card["id"]) is False
        await asyncio.sleep(1.5)  # nothing moves while the operator has the window
        assert await mid_run.p.fetchval("select status from subtasks where id = $1", card["id"]) == "needs_human"
        assert await hitl.respond(card["id"], "done")  # "Give control back"
        t = await wait_for(lambda: finished(task["id"]), timeout=40)
    b1 = (await subtasks(task["id"]))["b1"]
    assert t["status"] == "done" and b1["status"] == "done", b1["error"]
    logged = [e["message"] for e in await mid_run.p.fetch("select message from events where type = 'log' and level = 'warn'")]
    assert any("You took over this step" in m for m in logged)


async def test_stopping_from_the_take_over_card_ends_the_step(mid_run):
    task = await mid_run.run("compose.html")
    async with running_fleet():
        card = await mid_run.card()
        assert await hitl.respond(card["id"], "reject")
        await wait_for(lambda: finished(task["id"]), timeout=40)
    b1 = (await subtasks(task["id"]))["b1"]
    assert b1["status"] == "failed" and "operator stopped" in b1["error"]


async def test_without_a_request_the_worker_never_pauses(env, monkeypatch):  # noqa: F811
    async def field_text(goal, action, page, history, *, model, usage):
        return "Hello"

    monkeypatch.setattr(decide, "choose", scripted([("fill", "Message"), "DONE"], []))
    monkeypatch.setattr(decide, "field_text", field_text)
    task = await env.run("compose.html")
    async with running_fleet():
        t = await wait_for(lambda: finished(task["id"]), timeout=40)
    assert t["status"] == "done"
    assert await env.p.fetchval("select count(*) from events where type = 'human.request'") == 0
