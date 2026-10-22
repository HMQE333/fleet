"""The cost report (gate C4): spend per role for a period, first run vs replays per automation."""

import pytest
from httpx import ASGITransport, AsyncClient

from fleet import costs_report, db
from fleet.api import create_app

PANEL = {"X-Fleet-Client": "panel"}


@pytest.fixture
async def client():
    app = create_app(run_background=False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8710") as c:
        yield c


async def task(cost: float, *, automation_id=None, days_ago: int = 0) -> str:
    p = await db.pool()
    return await p.fetchval(
        "insert into tasks (prompt, status, cost_usd, automation_id, created_at) "
        "values ('x', 'done', $1, $2, now() - make_interval(days => $3)) returning id",
        cost, automation_id, days_ago)


async def step(task_id, kind: str, cost: float) -> None:
    p = await db.pool()
    await p.execute(
        "insert into subtasks (task_id, round, key, title, kind, instructions, cost_usd, status) "
        "values ($1, 0, $2, 'x', $3, 'x', $4, 'done')", task_id, f"{kind}-{cost}", kind, cost)


def role(body: dict, name: str) -> dict:
    return next(r for r in body["by_role"] if r["role"] == name)


async def test_spend_per_role_comes_from_the_steps_and_the_rest_is_the_planner():
    t = await task(1.00)
    await step(t, "browser_fast", 0.10)
    await step(t, "browser_fast", 0.20)
    await step(t, "fetch", 0.05)
    body = await costs_report.summary(30)
    assert role(body, "browser_fast") == {"role": "browser_fast", "usd": 0.3, "steps": 2}
    assert role(body, "fetch") == {"role": "fetch", "usd": 0.05, "steps": 1}
    assert role(body, "planner")["usd"] == pytest.approx(0.65)  # $1.00 bill, $0.35 of it in steps
    assert body["total_usd"] == 1.0 and body["tasks"] == 1
    assert [r["role"] for r in body["by_role"]] == ["planner", "browser_fast", "fetch"]  # biggest spender first


async def test_the_planner_row_never_goes_negative_when_steps_cost_more_than_the_bill_says():
    t = await task(0.10)
    await step(t, "browser", 0.30)  # a task cancelled before its bill was charged
    body = await costs_report.summary(30)
    assert role(body, "planner")["usd"] == 0.0


async def test_the_period_is_counted_from_the_task_creation():
    await task(5.00, days_ago=40)
    await task(0.50, days_ago=3)
    body = await costs_report.summary(30)
    assert body["total_usd"] == 0.5 and body["days"] == 30
    assert costs_report.summary.__doc__  # the docstring says what "period" means
    body = await costs_report.summary(60)
    assert body["total_usd"] == 5.5


async def test_an_automation_compares_its_first_run_with_its_replays():
    p = await db.pool()
    first = await task(0.80)
    auto = await p.fetchval(
        "insert into automations (name, prompt, steps, source_task_id) values ('nightly', 'x', '[]', $1) returning id", first)
    await task(0.05, automation_id=auto)
    await task(0.07, automation_id=auto)
    await task(0.50, automation_id=auto, days_ago=45)  # outside the period: not a replay of these 30 days
    body = await costs_report.summary(30)
    (a,) = body["automations"]
    assert a["automation_id"] == str(auto) and a["name"] == "nightly" and a["source_task_id"] == str(first)
    assert a["first_run_usd"] == 0.8 and a["replays"] == 2 and a["replay_total_usd"] == pytest.approx(0.12)
    assert a["saved_usd"] == pytest.approx(2 * 0.8 - 0.12)


async def test_replays_dearer_than_the_first_run_show_as_a_negative_saving():
    p = await db.pool()
    first = await task(0.10)
    auto = await p.fetchval(
        "insert into automations (name, prompt, steps, source_task_id) values ('drifted', 'x', '[]', $1) returning id", first)
    await task(0.40, automation_id=auto)
    (a,) = (await costs_report.summary(30))["automations"]
    assert a["saved_usd"] == pytest.approx(-0.3)


async def test_an_automation_without_runs_in_the_period_is_left_out_and_one_without_a_source_still_counts():
    p = await db.pool()
    old = await task(0.10, days_ago=90)
    await p.execute("insert into automations (name, prompt, steps, source_task_id) values ('old', 'x', '[]', $1)", old)
    orphan = await p.fetchval("insert into automations (name, prompt, steps) values ('orphan', 'x', '[]') returning id")
    await task(0.02, automation_id=orphan)
    body = await costs_report.summary(30)
    assert [a["name"] for a in body["automations"]] == ["orphan"]
    (a,) = body["automations"]
    assert a["first_run_usd"] == 0 and a["source_task_id"] is None and a["replays"] == 1 and a["saved_usd"] == -0.02


async def test_the_route_serves_the_summary_and_bounds_the_period(client):
    t = await task(0.25)
    await step(t, "scrape", 0.25)
    r = await client.get("/api/costs/summary?days=30", headers=PANEL)
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"days", "since", "total_usd", "tasks", "by_role", "automations"}
    assert role(body, "scrape")["usd"] == 0.25 and role(body, "planner")["usd"] == 0
    assert (await client.get("/api/costs/summary", headers=PANEL)).json()["days"] == 30
    assert (await client.get("/api/costs/summary?days=0", headers=PANEL)).status_code == 422
    assert (await client.get("/api/costs/summary?days=1000", headers=PANEL)).status_code == 422
