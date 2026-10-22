"""C4: the bill is visible: spend per role for a period, and first run vs replays per automation."""

import pytest
from httpx import ASGITransport, AsyncClient

from fleet import db
from fleet.api import create_app

HEADERS = {"X-Fleet-Client": "panel", "Origin": "http://127.0.0.1:8710", "Host": "127.0.0.1:8710"}


@pytest.fixture
async def client():
    app = create_app(run_background=False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8710") as c:
        yield c  # the clean_db fixture migrated already; no lifespan (it would open a second LISTEN connection)


async def test_the_summary_shows_roles_and_replay_savings(client):
    p = await db.pool()
    first = await p.fetchrow("insert into tasks (prompt, status, cost_usd) values ('x', 'done', 0.50) returning id")
    auto = await p.fetchrow(
        "insert into automations (name, prompt, steps, source_task_id) values ('A', 'x', '[]', $1) returning id", first["id"])
    await p.execute("insert into tasks (prompt, status, cost_usd, automation_id) values ('x', 'done', 0.02, $1)", auto["id"])
    await p.execute("insert into tasks (prompt, status, cost_usd, automation_id) values ('x', 'done', 0.03, $1)", auto["id"])
    r = await client.get("/api/costs/summary?days=30", headers=HEADERS)
    assert r.status_code == 200, r.text
    body = r.json()
    assert "by_role" in body and "automations" in body
    a = next(x for x in body["automations"] if x["automation_id"] == str(auto["id"]))
    assert a["first_run_usd"] == 0.5 and a["replays"] == 2 and abs(a["replay_total_usd"] - 0.05) < 1e-6
    assert a["saved_usd"] > 0.9  # 2 replays × $0.50 that didn't have to be spent
