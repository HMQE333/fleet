"""Retrying one failed step of a finished task: the step and what was skipped because of it start over, everything that worked stays.

The rules under test: only a failed step of the latest round of a finished task; never a step that already did something irreversible;
dependents (and theirs) restart, unrelated steps are left exactly as they were; the planner reviews again afterwards.
"""

import httpx
import pytest

from fleet import conductor, db
from fleet.api import create_app

from .conftest import finished, new_task, running_fleet, sub, subtasks, wait_for

PANEL = {"X-Fleet-Client": "panel"}
UNKNOWN = "00000000-0000-0000-0000-000000000000"


@pytest.fixture
async def client():
    app = create_app(run_background=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8710") as c:
        yield c


async def _failed_task(fake):
    """s1 fails; s2 and s4 (which depends on s2) are skipped because of it; s3 is independent and works; s6 fails for its
    own reasons and s7 is skipped because of THAT."""
    fake.plan.subtasks = [sub("s1"), sub("s2", depends_on=["s1"]), sub("s3"), sub("s4", depends_on=["s2"]), sub("s6"), sub("s7", depends_on=["s6"])]
    fake.fail_keys = {"s1", "s6"}
    task = await new_task()
    async with running_fleet():
        await wait_for(lambda: finished(task["id"]), timeout=40)
    fake.fail_keys = {"s6"}  # whatever goes wrong with s6 keeps going wrong
    return task, await subtasks(task["id"])


async def test_a_failed_step_and_what_it_blocked_run_again_and_the_rest_is_left_alone(fake, client):
    task, before = await _failed_task(fake)
    assert before["s1"]["status"] == "failed" and before["s2"]["status"] == "skipped" and before["s4"]["status"] == "skipped"
    assert before["s3"]["status"] == "done"
    r = await client.post(f"/api/subtasks/{before['s1']['id']}/retry", headers=PANEL)
    assert r.status_code == 200 and r.json()["also_restarted"] == 2
    p = await db.pool()
    assert await p.fetchval("select status from tasks where id = $1", task["id"]) == "running"
    fresh = await _row(task["id"], "s1")  # before any worker touched it again: a clean slate
    assert (fresh["status"], fresh["error"], fresh["result"], fresh["attempts"], fresh["finished_at"]) == ("pending", None, None, 0, None)
    async with running_fleet():
        t = await wait_for(lambda: finished(task["id"]), timeout=40)
    after = await subtasks(task["id"])
    assert t["status"] == "done" and all(after[k]["status"] == "done" for k in ("s1", "s2", "s3", "s4"))
    assert after["s6"]["status"] == "failed" and after["s7"]["status"] == "skipped"  # nothing to do with s1: left as they were
    assert after["s1"]["error"] is None and after["s1"]["result"] == "result of s1"
    assert after["s3"]["finished_at"] == before["s3"]["finished_at"] and after["s3"]["attempts"] == before["s3"]["attempts"]  # untouched
    assert t["result"] == "done: s1,s2,s3,s4"  # the planner reviewed again with everything in hand
    assert await p.fetchval("select count(*) from events where type = 'subtask.retry'") == 1


async def test_a_step_can_be_retried_more_than_once(fake, client):
    task, before = await _failed_task(fake)
    fake.fail_keys = {"s1", "s6"}  # still failing the second time
    assert (await client.post(f"/api/subtasks/{before['s1']['id']}/retry", headers=PANEL)).status_code == 200
    async with running_fleet():
        await wait_for(lambda: finished(task["id"]), timeout=40)
    assert (await subtasks(task["id"]))["s1"]["status"] == "failed"
    fake.fail_keys = {"s6"}
    assert (await client.post(f"/api/subtasks/{before['s1']['id']}/retry", headers=PANEL)).status_code == 200
    async with running_fleet():
        t = await wait_for(lambda: finished(task["id"]), timeout=40)
    assert (await subtasks(task["id"]))["s1"]["status"] == "done" and t["status"] == "done"


async def _row(task_id, key):
    p = await db.pool()
    return await p.fetchrow("select * from subtasks where task_id = $1 and key = $2", task_id, key)


async def test_only_a_failed_step_of_a_finished_tasks_latest_round_can_be_retried(fake, client):
    task, steps = await _failed_task(fake)
    p = await db.pool()
    done = steps["s3"]
    assert (await client.post(f"/api/subtasks/{done['id']}/retry", headers=PANEL)).status_code == 409  # it worked
    assert (await client.post(f"/api/subtasks/{steps['s2']['id']}/retry", headers=PANEL)).status_code == 409  # skipped, not failed
    assert (await client.post(f"/api/subtasks/{UNKNOWN}/retry", headers=PANEL)).status_code == 409
    assert (await client.post(f"/api/subtasks/{steps['s1']['id']}/retry")).status_code == 403  # CSRF guard
    await p.execute("update tasks set status = 'running' where id = $1", task["id"])
    r = await client.post(f"/api/subtasks/{steps['s1']['id']}/retry", headers=PANEL)
    assert r.status_code == 409 and "still running" in r.json()["detail"]
    await p.execute("update tasks set status = 'cancelled' where id = $1", task["id"])
    assert (await client.post(f"/api/subtasks/{steps['s1']['id']}/retry", headers=PANEL)).status_code == 409  # stopped by the operator
    await p.execute("update tasks set status = 'done', round = 2 where id = $1", task["id"])
    r = await client.post(f"/api/subtasks/{steps['s1']['id']}/retry", headers=PANEL)
    assert r.status_code == 409 and "earlier round" in r.json()["detail"]
    assert (await _row(task["id"], "s1"))["status"] == "failed"  # nothing above changed it


async def test_a_step_that_already_did_something_irreversible_is_never_repeated(fake, client):
    task, steps = await _failed_task(fake)
    p = await db.pool()
    await p.execute("update subtasks set side_effects = true where id = $1", steps["s1"]["id"])
    r = await client.post(f"/api/subtasks/{steps['s1']['id']}/retry", headers=PANEL)
    assert r.status_code == 409 and "can't be undone" in r.json()["detail"]
    assert (await _row(task["id"], "s1"))["status"] == "failed"
    assert await p.fetchval("select status from tasks where id = $1", task["id"]) != "running"


async def test_retry_of_the_step_in_a_saved_automations_run_blocks_the_next_run_until_it_finishes(fake):
    from fleet import automations

    task, steps = await _failed_task(fake)
    p = await db.pool()
    await p.execute("update tasks set status = 'done' where id = $1", task["id"])
    a = await automations.create_from_task(task["id"], "A", None, "UTC")
    run = await automations.start_run(a["id"])
    await p.execute("update subtasks set status = 'failed', error = 'x', finished_at = now() where task_id = $1 and key = 's3'", run["id"])
    await p.execute("update tasks set status = 'failed', finished_at = now() where id = $1", run["id"])
    row = await p.fetchrow("select id from subtasks where task_id = $1 and key = 's3'", run["id"])
    await conductor.retry_subtask(row["id"])
    assert await automations.start_run(a["id"]) is None  # a new run must wait: the last one is running again
    assert await p.fetchval("select status from tasks where id = $1", run["id"]) == "running"
