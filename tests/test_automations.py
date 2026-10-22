"""Saved automations and schedules."""

import asyncio
from datetime import UTC, datetime

import pytest

from fleet import automations, db, planner, schedules
from fleet.planner import Review

from .conftest import finished, new_task, running_fleet, sub, subtasks, wait_for

WARSAW = "Europe/Warsaw"


# -- schedule arithmetic (no database) ------------------------------------------------


@pytest.mark.no_db
def test_weekday_schedule_skips_the_weekend():
    spec = schedules.validate({"type": "daily", "time": "09:00", "days": [1, 2, 3, 4, 5]})
    friday_after = datetime(2026, 9, 25, 8, 30, tzinfo=UTC)  # Fri 10:30 in Warsaw (UTC+2)
    assert schedules.next_run(spec, WARSAW, friday_after) == datetime(2026, 9, 28, 7, 0, tzinfo=UTC)  # Mon 09:00


@pytest.mark.no_db
def test_same_day_slot_and_daylight_saving_change():
    spec = schedules.validate({"type": "daily", "time": "09:00"})
    early = datetime(2026, 9, 25, 5, 0, tzinfo=UTC)  # 07:00 Warsaw
    assert schedules.next_run(spec, WARSAW, early) == datetime(2026, 9, 25, 7, 0, tzinfo=UTC)
    # Clocks go back on 25 Oct 2026: 09:00 Warsaw is 08:00 UTC from then on.
    assert schedules.next_run(spec, WARSAW, datetime(2026, 10, 24, 12, 0, tzinfo=UTC)) == datetime(2026, 10, 25, 8, 0, tzinfo=UTC)


@pytest.mark.no_db
def test_hourly_and_validation():
    spec = schedules.validate({"type": "hourly", "every": 3})
    assert schedules.next_run(spec, "UTC", datetime(2026, 9, 25, 10, 40, tzinfo=UTC)) == datetime(2026, 9, 25, 13, 0, tzinfo=UTC)
    assert schedules.describe(schedules.validate({"type": "daily", "time": "07:30", "days": [1, 2, 3, 4, 5]}), WARSAW) \
        == "Weekdays at 07:30 (Europe/Warsaw)"
    for bad in ({"type": "daily", "time": "9am"}, {"type": "hourly", "every": 0}, {"type": "cron"}):
        with pytest.raises(schedules.ScheduleError):
            schedules.validate(bad)
    with pytest.raises(schedules.ScheduleError):
        schedules.zone("Mars/Olympus")


# -- automations (database) ------------------------------------------------------------


async def _finished_task_with_a_failed_step(fake):
    fake.plan.subtasks = [sub("s1"), sub("s2"), sub("s3", depends_on=["s1", "s2"])]
    fake.fail_keys = {"s2"}
    task = await new_task(prompt="Check the prices")
    async with running_fleet():
        await wait_for(lambda: finished(task["id"]))
    fake.fail_keys = set()
    return task


async def test_saving_keeps_only_the_steps_that_worked(fake):
    task = await _finished_task_with_a_failed_step(fake)
    p = await db.pool()
    await p.execute("update tasks set status = 'done' where id = $1", task["id"])
    a = await automations.create_from_task(task["id"], "Prices", {"type": "daily", "time": "09:00"}, WARSAW)
    keys = [s["key"] for s in a["steps"]]
    assert "s2" not in keys and "s1" in keys  # s2 failed, s3 was skipped because of it
    assert a["next_run_at"] is not None


async def test_manual_run_replays_steps_without_the_planner(fake, monkeypatch):
    fake.plan.subtasks = [sub("s1"), sub("s2", depends_on=["s1"])]
    task = await new_task(prompt="Collect the numbers")
    async with running_fleet():
        await wait_for(lambda: finished(task["id"]))
    a = await automations.create_from_task(task["id"], "Numbers", None, "UTC")
    assert a["next_run_at"] is None  # manual only

    async def planner_must_not_run(*args, **kwargs):
        raise AssertionError("an automation run must not re-plan")

    monkeypatch.setattr(planner, "plan", planner_must_not_run)
    fake.seen_deps.clear()
    run = await automations.start_run(a["id"])
    assert run["status"] == "running" and run["plan_review"] == "never"
    async with running_fleet():
        t = await wait_for(lambda: finished(run["id"]))
    assert t["status"] == "done"
    assert fake.seen_deps["s2"] == {"s1": "result of s1"}  # dependencies replayed too


async def test_runs_never_overlap(fake, monkeypatch):
    fake.plan.subtasks = [sub("s1")]
    task = await new_task()
    async with running_fleet():
        await wait_for(lambda: finished(task["id"]))
    a = await automations.create_from_task(task["id"], "One", None, "UTC")
    first = await automations.start_run(a["id"])
    assert first is not None
    assert await automations.start_run(a["id"]) is None  # first run hasn't finished


async def test_a_due_schedule_starts_exactly_one_run(fake):
    fake.plan.subtasks = [sub("s1")]
    task = await new_task()
    async with running_fleet():
        await wait_for(lambda: finished(task["id"]))
    a = await automations.create_from_task(task["id"], "Hourly", {"type": "hourly", "every": 1}, "UTC")
    p = await db.pool()
    await p.execute("update automations set next_run_at = now() - interval '3 hours' where id = $1", a["id"])
    # Two conductors wake up at the same moment:
    counts = await asyncio.gather(automations.run_due(), automations.run_due())
    assert sum(counts) == 1
    runs = await p.fetchval("select count(*) from tasks where automation_id = $1", a["id"])
    nxt = await p.fetchval("select next_run_at from automations where id = $1", a["id"])
    assert runs == 1 and nxt > datetime.now(UTC)  # missed slots are not replayed


async def test_a_deleted_profile_fails_that_step_clearly(fake):
    p = await db.pool()
    await p.execute("insert into profiles (adspower_id, name, enabled) values ('k1', 'Shop account', true)")
    fake.plan.subtasks = [sub("b1", kind="browser", profile="Shop account")]
    task = await new_task()
    async with running_fleet():
        await wait_for(lambda: finished(task["id"]))
    a = await automations.create_from_task(task["id"], "Shop", None, "UTC")
    await p.execute("delete from profiles")
    run = await automations.start_run(a["id"])
    b1 = (await subtasks(run["id"]))["b1"]
    assert b1["status"] == "failed" and "no longer exists" in b1["error"]


async def test_unfinished_tasks_cannot_be_saved():
    task = await new_task()
    with pytest.raises(automations.AutomationError, match="finished successfully"):
        await automations.create_from_task(task["id"], "x", None, "UTC")


# -- a saved run of tables needs no model to be summed up (found in a real run: it cost a planner call per run) ----------------------


async def _table_task(fake, monkeypatch, *, kinds=("scrape",), fail_second_run=False):
    """A finished task of table steps, saved as an automation; returns (automation, calls of the planner's review)."""
    from fleet import planner, worker
    from fleet.executors import Blocked, ExecResult

    state = {"runs": 0, "reviews": 0}

    async def table(ctx):
        if fail_second_run and state["runs"] >= 1:
            raise Blocked("the site said no")
        return ExecResult(result=f"Scraped 3 items for {ctx.subtask['key']}")

    monkeypatch.setitem(worker.EXECUTORS, "scrape", table)
    fake.plan.subtasks = [sub(f"s{i + 1}", kind=k, url="https://example.com") for i, k in enumerate(kinds)]
    task = await new_task(prompt="Watch the prices")
    async with running_fleet():
        await wait_for(lambda: finished(task["id"]))
    a = await automations.create_from_task(task["id"], "Tables", None, "UTC")

    async def counted_review(*args, **kwargs):
        state["reviews"] += 1
        return Review(decision="finish", answer="model answer", subtasks=[])

    monkeypatch.setattr(planner, "review", counted_review)
    state["runs"] = 1
    return a, state


async def test_a_saved_run_of_only_tables_is_summed_up_without_a_model(fake, monkeypatch):
    a, state = await _table_task(fake, monkeypatch, kinds=("scrape", "scrape"))
    run = await automations.start_run(a["id"])
    async with running_fleet():
        t = await wait_for(lambda: finished(run["id"]))
    assert t["status"] == "done" and state["reviews"] == 0
    assert t["result"] == "### Do s1\nScraped 3 items for s1\n\n### Do s2\nScraped 3 items for s2"
    assert float(t["cost_usd"]) == 0.0  # nothing was spent on a model


async def test_a_failed_table_step_still_gets_a_review_to_explain_it(fake, monkeypatch):
    a, state = await _table_task(fake, monkeypatch, fail_second_run=True)
    run = await automations.start_run(a["id"])
    async with running_fleet():
        t = await wait_for(lambda: finished(run["id"]))
    assert state["reviews"] == 1 and t["result"] == "model answer"


async def test_a_saved_run_that_mixes_in_other_steps_is_reviewed_as_before(fake, monkeypatch):
    a, state = await _table_task(fake, monkeypatch, kinds=("scrape", "fetch"))
    run = await automations.start_run(a["id"])
    async with running_fleet():
        t = await wait_for(lambda: finished(run["id"]))
    assert state["reviews"] == 1 and t["result"] == "model answer"


async def test_a_plain_task_of_tables_is_still_reviewed(fake, monkeypatch):
    from fleet import planner, worker
    from fleet.executors import ExecResult

    async def table(ctx):
        return ExecResult(result="Scraped 3 items")

    calls = []

    async def counted_review(*args, **kwargs):
        calls.append(1)
        return Review(decision="finish", answer="model answer", subtasks=[])

    monkeypatch.setitem(worker.EXECUTORS, "scrape", table)
    monkeypatch.setattr(planner, "review", counted_review)
    fake.plan.subtasks = [sub("s1", kind="scrape", url="https://example.com")]
    task = await new_task()
    async with running_fleet():
        t = await wait_for(lambda: finished(task["id"]))
    assert calls == [1] and t["result"] == "model answer"  # the operator asked in their own words: the planner answers


@pytest.mark.no_db
def test_the_planner_is_told_that_a_tables_hidden_rows_are_complete_and_are_not_to_be_re_read():
    from fleet import prompts, scrape

    assert "COMPLETE tables" in prompts.REVIEW_SYSTEM and "not captured" in prompts.REVIEW_SYSTEM
    rows = [{"a": str(i)} for i in range(30)]
    text = scrape.summary(["a"], rows, pages=2, url="https://x.test", fill={"a": 1.0}, reused=False, more_pages=False)
    assert "15 more rows. The table is complete" in text and "Don't re-read these pages" in text
