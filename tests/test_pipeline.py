"""End-to-end pipeline tests with stand-in planner and executors (no API calls, no browser)."""

import asyncio

from fleet import conductor, db, hitl, worker
from fleet.executors import ExecResult
from fleet.planner import Plan, Review

from .conftest import finished, new_task, running_fleet, sub, subtasks, task_status, wait_for


async def test_parallel_subtasks_then_dependent_then_done(fake):
    fake.plan.subtasks = [sub("s1"), sub("s2", kind="research"), sub("s3", depends_on=["s1", "s2"])]
    task = await new_task()
    async with running_fleet():
        t = await wait_for(lambda: finished(task["id"]))
    assert t["status"] == "done"
    assert t["result"] == "done: s1,s2,s3"
    assert fake.seen_deps["s3"] == {"s1": "result of s1", "s2": "result of s2"}
    assert fake.max_concurrent >= 2  # s1 and s2 ran at the same time


async def test_failed_dependency_skips_dependents(fake):
    fake.plan.subtasks = [sub("s1"), sub("s2", depends_on=["s1"]), sub("s3")]
    fake.fail_keys = {"s1"}
    task = await new_task()
    async with running_fleet():
        await wait_for(lambda: finished(task["id"]))
    subs = await subtasks(task["id"])
    assert subs["s1"]["status"] == "failed" and "site said no" in subs["s1"]["error"]
    assert subs["s2"]["status"] == "skipped"
    assert subs["s3"]["status"] == "done"


async def test_refusal_fails_task(fake):
    fake.plan = Plan(decision="refuse", reason="Rule 1: fake personas", summary="", subtasks=[])
    task = await new_task()
    async with running_fleet():
        t = await wait_for(lambda: finished(task["id"]))
    assert t["status"] == "failed" and "fake personas" in t["error"]


async def test_review_can_add_a_second_round(fake):
    fake.plan.subtasks = [sub("s1")]
    fake.reviews = [Review(decision="continue", answer="need more", subtasks=[sub("s2", depends_on=["s1"])])]
    task = await new_task()
    async with running_fleet():
        t = await wait_for(lambda: finished(task["id"]))
    subs = await subtasks(task["id"])
    assert t["status"] == "done" and t["round"] == 2
    assert subs["s2"]["round"] == 2
    assert fake.seen_deps["s2"] == {"s1": "result of s1"}


async def test_dependency_cycle_is_skipped_not_stuck(fake):
    fake.plan.subtasks = [sub("s1", depends_on=["s2"]), sub("s2", depends_on=["s1"]), sub("s3")]
    task = await new_task()
    async with running_fleet():
        t = await wait_for(lambda: finished(task["id"]), timeout=30)
    subs = await subtasks(task["id"])
    assert t["status"] == "done"
    assert subs["s1"]["status"] == subs["s2"]["status"] == "skipped"


async def test_cancel_stops_everything(fake):
    fake.plan.subtasks = [sub("s1"), sub("s2", depends_on=["s1"])]
    task = await new_task()
    async with running_fleet():
        await wait_for(lambda: subtasks(task["id"]))
        assert await conductor.cancel_task(task["id"])
        await asyncio.sleep(1)
    t = await task_status(task["id"])
    subs = await subtasks(task["id"])
    assert t["status"] == "cancelled"
    assert subs["s2"]["status"] == "cancelled"


async def test_human_in_the_loop_round_trip(fake, monkeypatch):
    answers = {}

    async def needs_approval(ctx):
        answers["resp"] = await hitl.ask(ctx.subtask, "approval", "Send the email?")
        return ExecResult(result=f"operator said {answers['resp']['decision']}")

    monkeypatch.setitem(worker.EXECUTORS, "fetch", needs_approval)
    fake.plan.subtasks = [sub("s1")]
    task = await new_task()
    p = await db.pool()
    async with running_fleet():
        st = await wait_for(lambda: p.fetchrow("select * from subtasks where status = 'needs_human'"))
        assert st["human_request"]["kind"] == "approval"
        assert await hitl.respond(st["id"], "approve", "go ahead")
        assert not await hitl.respond(st["id"], "reject", "double click")  # second answer is refused
        t = await wait_for(lambda: finished(task["id"]))
    subs = await subtasks(task["id"])
    assert t["status"] == "done"
    assert subs["s1"]["result"] == "operator said approve"
    assert answers["resp"]["note"] == "go ahead"


async def test_one_profile_runs_one_browser_subtask_at_a_time(fake):
    p = await db.pool()
    await p.execute("insert into profiles (adspower_id, name, enabled, general_use) values ('k1', 'main', true, true), ('k2', 'off', false, true)")
    fake.plan.subtasks = [sub("b1", kind="browser"), sub("b2", kind="browser"), sub("b3", kind="browser")]
    task = await new_task()
    async with running_fleet():
        t = await wait_for(lambda: finished(task["id"]), timeout=30)
    subs = await subtasks(task["id"])
    assert t["status"] == "done"
    assert fake.max_concurrent == 1  # only one enabled profile
    prof = await p.fetchrow("select * from profiles where adspower_id = 'k1'")
    assert {s["profile_id"] for s in subs.values()} == {prof["id"]}
    assert prof["status"] == "idle" and prof["lease_owner"] is None


async def test_browser_subtask_without_profiles_fails_fast(fake):
    fake.plan.subtasks = [sub("b1", kind="browser"), sub("s2")]
    task = await new_task()
    async with running_fleet():
        t = await wait_for(lambda: finished(task["id"]))
    subs = await subtasks(task["id"])
    assert subs["b1"]["status"] == "failed" and "No browser profiles" in subs["b1"]["error"]
    assert t["status"] == "done"


async def test_budget_cap_stops_new_work(fake, monkeypatch):
    async def expensive(ctx):
        ctx.usage.cost_usd = 1.0
        return ExecResult(result="costly")

    monkeypatch.setitem(worker.EXECUTORS, "fetch", expensive)
    fake.plan.subtasks = [sub("s1"), sub("s2", depends_on=["s1"])]
    p = await db.pool()
    task = dict(await p.fetchrow("insert into tasks (prompt, budget_usd) values ('x', 0.5) returning *"))
    async with running_fleet():
        t = await wait_for(lambda: finished(task["id"]))
    subs = await subtasks(task["id"])
    assert t["status"] == "done"
    assert float(t["cost_usd"]) == 1.0
    assert subs["s2"]["status"] == "skipped" and "budget" in subs["s2"]["error"]


async def test_browser_subtask_on_broken_profile_fails_instead_of_waiting_forever(fake):
    p = await db.pool()
    await p.execute("insert into profiles (adspower_id, name, enabled) values ('k1', 'main', true)")
    fake.plan.subtasks = [sub("b1", kind="browser", profile="main")]
    task = await new_task()
    await p.execute("update profiles set status = 'error', last_error = 'AdsPower: profile deleted'")
    async with running_fleet():
        t = await wait_for(lambda: finished(task["id"]))
    subs = await subtasks(task["id"])
    assert t["status"] == "done"
    assert subs["b1"]["status"] == "failed" and "Profiles page" in subs["b1"]["error"]


async def test_retry_is_not_pinned_to_the_first_profile(fake, monkeypatch):
    p = await db.pool()
    await p.execute("insert into profiles (adspower_id, name, enabled, general_use) values ('k1', 'a', true, true), ('k2', 'b', true, true)")
    from fleet.executors.browser import ProfileError

    async def flaky(ctx):
        if ctx.profile["name"] == "a":
            raise ProfileError("AdsPower: failed to start")
        return ExecResult(result=f"ran on {ctx.profile['name']}")

    monkeypatch.setitem(worker.EXECUTORS, "browser", flaky)
    monkeypatch.setattr(worker, "_RETRY_DELAY_PROFILE", 0)
    fake.plan.subtasks = [sub("b1", kind="browser")]
    task = await new_task()
    async with running_fleet():
        t = await wait_for(lambda: finished(task["id"]), timeout=60)
    subs = await subtasks(task["id"])
    assert t["status"] == "done"
    assert subs["b1"]["status"] == "done" and subs["b1"]["result"] == "ran on b"


async def test_plan_that_uses_accounts_waits_for_the_operator(fake):
    p = await db.pool()
    await p.execute("insert into profiles (adspower_id, name, enabled, general_use) values ('k1', 'main', true, true)")
    fake.plan.subtasks = [sub("b1", kind="browser"), sub("s2")]
    task = await new_task(plan_review="auto")
    async with running_fleet():
        await wait_for(lambda: p.fetchval("select status = 'awaiting_approval' from tasks where id = $1", task["id"]))
        await asyncio.sleep(2)  # several conductor/worker ticks: nothing may start meanwhile
        started = await p.fetchval(
            "select count(*) from subtasks where task_id = $1 and status <> 'pending'", task["id"])
        assert started == 0 and fake.max_concurrent == 0
        assert await conductor.approve_plan(task["id"])
        assert not await conductor.approve_plan(task["id"])  # double click
        t = await wait_for(lambda: finished(task["id"]))
    assert t["status"] == "done"


async def test_read_only_plan_runs_without_asking(fake):
    fake.plan.subtasks = [sub("s1"), sub("s2", kind="research")]
    task = await new_task(plan_review="auto")
    async with running_fleet():
        t = await wait_for(lambda: finished(task["id"]))
    assert t["status"] == "done"


async def test_always_review_asks_even_for_read_only_plans(fake):
    fake.plan.subtasks = [sub("s1")]
    task = await new_task(plan_review="always")
    p = await db.pool()
    async with running_fleet():
        await wait_for(lambda: p.fetchval("select status = 'awaiting_approval' from tasks where id = $1", task["id"]))
        assert await conductor.cancel_task(task["id"])
    subs = await subtasks(task["id"])
    assert subs["s1"]["status"] == "cancelled"


async def test_subtask_that_already_acted_is_never_retried(fake, monkeypatch):
    async def acted_then_crashed(ctx):
        p = await db.pool()
        await p.execute("update subtasks set side_effects = true where id = $1", ctx.subtask["id"])
        raise RuntimeError("browser crashed after sending the message")

    monkeypatch.setitem(worker.EXECUTORS, "fetch", acted_then_crashed)
    fake.plan.subtasks = [sub("s1")]
    task = await new_task()
    async with running_fleet():
        await wait_for(lambda: finished(task["id"]))
    s1 = (await subtasks(task["id"]))["s1"]
    assert s1["status"] == "failed" and s1["attempts"] == 1  # max_attempts is 2, but it must not retry
    assert "irreversible" in s1["error"] and "browser crashed" in s1["error"]


async def test_unnamed_browser_work_never_lands_on_an_account_profile(fake):
    p = await db.pool()
    await p.execute(
        "insert into profiles (adspower_id, name, enabled, general_use) values "
        "('acc', 'Personal Gmail', true, false), ('gen', 'Scratch browser', true, true)"
    )
    fake.plan.subtasks = [sub("b1", kind="browser"), sub("b2", kind="browser"), sub("b3", kind="browser", profile="Personal Gmail")]
    task = await new_task()
    async with running_fleet():
        await wait_for(lambda: finished(task["id"]), timeout=30)
    subs = await subtasks(task["id"])
    names = {r["id"]: r["name"] for r in await p.fetch("select id, name from profiles")}
    assert names[subs["b1"]["profile_id"]] == names[subs["b2"]["profile_id"]] == "Scratch browser"
    assert names[subs["b3"]["profile_id"]] == "Personal Gmail"  # only because the plan named it


async def test_unnamed_browser_work_fails_clearly_without_a_general_profile(fake):
    p = await db.pool()
    await p.execute("insert into profiles (adspower_id, name, enabled) values ('acc', 'Personal Gmail', true)")
    fake.plan.subtasks = [sub("b1", kind="browser")]
    task = await new_task()
    async with running_fleet():
        t = await wait_for(lambda: finished(task["id"]))
    b1 = (await subtasks(task["id"]))["b1"]
    assert t["status"] == "done"
    assert b1["status"] == "failed" and "general browsing" in b1["error"] and fake.max_concurrent == 0
