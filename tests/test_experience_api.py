"""Reading and pruning the experience base (gate K4): list per site, newest first, capped; sites with counts; forget one."""

import pytest
from httpx import ASGITransport, AsyncClient

from fleet import db, experience_api
from fleet.api import create_app

PANEL = {"X-Fleet-Client": "panel"}


@pytest.fixture
async def client():
    app = create_app(run_background=False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8710") as c:
        yield c


async def learned(domain: str, goal: str, status: str = "done", *, minutes_ago: int = 0, actions=None, reason: str = "") -> str:
    p = await db.pool()
    return await p.fetchval(
        "insert into experience (domain, goal, kind, status, reason, actions, cost_usd, seconds, created_at) "
        "values ($1, $2, 'browser_fast', $3, $4, $5, 0.01, 2, now() - make_interval(mins => $6)) returning id",
        domain, goal, status, reason, actions or [], minutes_ago)


async def test_entries_are_per_site_newest_first_with_every_column():
    old = await learned("shop.test", "older", minutes_ago=10, actions=[{"kind": "click", "label": "Next"}], reason="fine")
    new = await learned("shop.test", "newer")
    await learned("other.test", "elsewhere")
    rows = await experience_api.entries("shop.test")
    assert [r["id"] for r in rows] == [new, old]
    assert set(rows[1]) == {"id", "domain", "goal", "kind", "status", "reason", "actions", "cost_usd", "seconds", "task_id",
                            "subtask_id", "created_at"}
    assert rows[1]["actions"] == [{"kind": "click", "label": "Next"}] and rows[1]["reason"] == "fine"
    everything = await experience_api.entries(None)  # no domain: every site, still newest first
    assert {r["goal"] for r in everything} == {"newer", "elsewhere", "older"} and everything[-1]["goal"] == "older"


async def test_the_domain_is_matched_as_written_down_lower_case_and_trimmed():
    await learned("shop.test", "x")
    assert len(await experience_api.entries(" Shop.TEST ")) == 1
    assert await experience_api.entries("shop") == []


async def test_a_listing_is_capped_at_200():
    p = await db.pool()
    await p.executemany(
        "insert into experience (domain, goal, kind, status) values ('big.test', $1, 'browser', 'done')",
        [(f"goal {i}",) for i in range(205)])
    assert len(await experience_api.entries("big.test")) == 200
    assert len(await experience_api.entries("big.test", limit=5)) == 5
    assert len(await experience_api.entries("big.test", limit=10_000)) == 200


async def test_sites_count_entries_and_successes():
    await learned("shop.test", "a", "done")
    await learned("shop.test", "b", "goal_achieved", minutes_ago=5)
    await learned("shop.test", "c", "stuck")
    await learned("other.test", "d", "failed")
    sites = await experience_api.sites()
    assert [(s["domain"], s["count"], s["succeeded"]) for s in sites] == [("shop.test", 3, 2), ("other.test", 1, 0)]
    assert float(sites[0]["cost_usd"]) == pytest.approx(0.03) and sites[0]["last_at"] is not None


async def test_forget_removes_one_entry_and_says_when_there_was_none():
    keep = await learned("shop.test", "keep")
    gone = await learned("shop.test", "gone")
    assert await experience_api.forget(gone) is True
    assert await experience_api.forget(gone) is False
    assert [r["id"] for r in await experience_api.entries("shop.test")] == [keep]


async def test_the_routes_list_count_and_forget(client):
    row = await learned("shop.test", "find a pi", actions=[{"kind": "fill", "label": "Search", "value": "pi"}])
    r = await client.get("/api/experience?domain=shop.test", headers=PANEL)
    assert r.status_code == 200
    (e,) = r.json()
    assert e["id"] == str(row) and e["goal"] == "find a pi" and e["actions"][0]["value"] == "pi" and e["cost_usd"] == 0.01
    assert (await client.get("/api/experience", headers=PANEL)).json()[0]["id"] == str(row)
    assert (await client.get("/api/experience?limit=0", headers=PANEL)).status_code == 422
    assert (await client.get("/api/experience?limit=201", headers=PANEL)).status_code == 422
    sites = (await client.get("/api/experience/sites", headers=PANEL)).json()
    assert sites == [{"domain": "shop.test", "count": 1, "succeeded": 1, "cost_usd": 0.01, "last_at": sites[0]["last_at"]}]
    assert (await client.delete(f"/api/experience/{row}")).status_code == 403  # CSRF guard: the panel header is needed
    assert (await client.delete(f"/api/experience/{row}", headers=PANEL)).status_code == 200
    assert (await client.delete(f"/api/experience/{row}", headers=PANEL)).status_code == 404
    assert (await client.get("/api/experience/sites", headers=PANEL)).json() == []
