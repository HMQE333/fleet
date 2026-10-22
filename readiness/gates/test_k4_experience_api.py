"""K4: the operator can see what Fleet learned and make it forget.

Expected shape: GET /api/experience?domain=shop.test (newest first, at most 200, every column but nothing secret),
GET /api/experience/sites (domains with counts), DELETE /api/experience/{id}; a Knowledge page in the panel that
calls them (checked statically). All guarded like every other route.
"""

import pathlib
import re

import pytest
from httpx import ASGITransport, AsyncClient

from fleet import db
from fleet.api import create_app

HEADERS = {"X-Fleet-Client": "panel", "Origin": "http://127.0.0.1:8710", "Host": "127.0.0.1:8710"}
WEB = pathlib.Path(__file__).resolve().parents[2] / "web" / "src"


@pytest.fixture
async def client():
    app = create_app(run_background=False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8710") as c:
        yield c


async def test_list_and_forget(client):
    p = await db.pool()
    row = await p.fetchrow(
        "insert into experience (domain, goal, kind, status, reason, actions, cost_usd, seconds) "
        "values ('shop.test', 'find a pi', 'browser_fast', 'done', '', '[]', 0.01, 3) returning id")
    r = await client.get("/api/experience?domain=shop.test", headers=HEADERS)
    assert r.status_code == 200 and [e["id"] for e in r.json()] == [str(row["id"])]
    r = await client.get("/api/experience/sites", headers=HEADERS)
    assert r.status_code == 200 and any(s["domain"] == "shop.test" and s["count"] == 1 for s in r.json())
    r = await client.delete(f"/api/experience/{row['id']}", headers=HEADERS)
    assert r.status_code in (200, 204)
    assert await p.fetchval("select count(*) from experience") == 0


async def test_the_panel_has_a_knowledge_page():
    text = "\n".join(f.read_text(encoding="utf-8") for f in WEB.rglob("*.tsx"))
    assert re.search(r"/api/experience", text), "no panel code calls /api/experience"
    assert re.search(r"Knowledge", (WEB / "App.tsx").read_text(encoding="utf-8")), "no Knowledge entry in the app navigation"
