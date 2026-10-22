"""Cost control: deep research is opt-in, budgets reserve expected costs, expensive plans ask first."""

import asyncio

from fleet import conductor, costs, db, llm, planner, worker
from fleet.executors import ExecResult
from fleet.planner import PlannedSubtask

from .conftest import finished, running_fleet, subtasks, wait_for


def research(key: str, tier: str = "smart", depends_on=()) -> PlannedSubtask:
    return PlannedSubtask(key=key, title=f"Research {key}", kind="research", instructions="look it up", start_url=None,
                          depends_on=list(depends_on), profile=None, model_tier=tier)


async def task_with(*, budget: float = 2.0, deep: bool = False, review: str = "never") -> dict:
    p = await db.pool()
    return dict(await p.fetchrow(
        "insert into tasks (prompt, budget_usd, allow_deep_research, plan_review) values ('x', $1, $2, $3) returning *",
        budget, deep, review))


async def test_deep_research_is_downgraded_unless_the_task_allows_it(fake):
    """What happened in the real run: the planner chose deep research for two quick follow-ups ($2.06)."""
    fake.plan.subtasks = [research("s1"), research("s2")]
    task = await task_with(deep=False)
    async with running_fleet():
        t = await wait_for(lambda: finished(task["id"]))
    subs = await subtasks(task["id"])
    assert t["status"] == "done"
    assert subs["s1"]["model_tier"] == subs["s2"]["model_tier"] == "fast"
    assert float(t["estimated_cost"]) == 2 * costs.expected("research", "fast")
    p = await db.pool()
    warnings = await p.fetchval("select count(*) from events where task_id = $1 and type = 'plan.warning'", task["id"])
    assert warnings == 2  # the operator can see why


async def test_allowed_deep_research_is_kept_and_an_expensive_plan_waits_for_ok(fake):
    fake.plan.subtasks = [research("s1")]
    task = await task_with(deep=True, review="auto")
    p = await db.pool()
    async with running_fleet():
        await wait_for(lambda: p.fetchval("select status = 'awaiting_approval' from tasks where id = $1", task["id"]))
        assert await conductor.cancel_task(task["id"])
    s1 = (await subtasks(task["id"]))["s1"]
    assert s1["model_tier"] == "smart" and float(s1["expected_cost"]) == costs.expected("research", "smart")


async def test_budget_reserves_expected_costs_so_expensive_steps_cannot_overshoot(fake, monkeypatch):
    """Two $1.50 steps on a $2 budget: the second must never start alongside the first, and is skipped after."""
    async def deep(ctx):
        fake._running += 1
        fake.max_concurrent = max(fake.max_concurrent, fake._running)
        try:
            await asyncio.sleep(0.5)
            ctx.usage.cost_usd = 1.20  # what a real deep research step cost
            return ExecResult(result="long report")
        finally:
            fake._running -= 1

    monkeypatch.setitem(worker.EXECUTORS, "research", deep)
    fake.plan.subtasks = [research("s1"), research("s2")]
    task = await task_with(budget=2.0, deep=True)
    async with running_fleet():
        t = await wait_for(lambda: finished(task["id"]))
    subs = await subtasks(task["id"])
    assert fake.max_concurrent == 1
    assert float(t["cost_usd"]) == 1.20  # not 2.40
    assert subs["s2"]["status"] == "skipped" and "budget" in subs["s2"]["error"] and "$1.5" in subs["s2"]["error"]


async def test_a_step_that_can_never_fit_is_skipped_up_front(fake):
    fake.plan.subtasks = [research("s1")]
    task = await task_with(budget=1.0, deep=True)  # 1.50 + reserve > 1.00
    async with running_fleet():
        t = await wait_for(lambda: finished(task["id"]))
    s1 = (await subtasks(task["id"]))["s1"]
    assert s1["status"] == "skipped" and float(t["cost_usd"]) == 0


async def test_a_failed_final_review_still_shows_what_the_workers_found(fake, monkeypatch):
    """The real run: review hit 'out of credits' and the operator got nothing."""
    async def broke(*a, **k):
        raise llm.LLMError("Your OpenRouter account is out of credits.")

    monkeypatch.setattr(planner, "review", broke)
    fake.plan.subtasks = [research("s1", tier="fast")]
    task = await task_with()
    async with running_fleet():
        t = await wait_for(lambda: finished(task["id"]))
    assert t["status"] == "failed" and "out of credits" in t["error"]
    assert "result of s1" in t["result"] and "Research s1" in t["result"]
