"""The local API drives logged-in browsers; these tests attack it the way a hostile web page would."""

import httpx
import pytest

from fleet.api import WEB_DIST, create_app

PANEL = {"X-Fleet-Client": "panel"}
TASK = {"prompt": "Publish a post saying hello", "require_approval": False}


@pytest.fixture
async def client():
    app = create_app(run_background=False)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8710") as c:
        yield c


async def test_panel_request_is_accepted(client):
    r = await client.post("/api/tasks", json=TASK, headers={**PANEL, "Origin": "http://127.0.0.1:8710"})
    assert r.status_code == 201


async def test_change_without_panel_header_is_refused(client):
    r = await client.post("/api/tasks", json=TASK)
    assert r.status_code == 403
    r = await client.post("/api/profiles/sync")  # body-less POSTs are the classic CSRF case
    assert r.status_code == 403


async def test_foreign_origin_is_refused_even_with_header(client):
    r = await client.post("/api/tasks", json=TASK, headers={**PANEL, "Origin": "https://evil.example"})
    assert r.status_code == 403


async def test_dns_rebinding_host_is_refused(client):
    r = await client.get("/api/tasks", headers={"Host": "rebind.evil.example"})
    assert r.status_code == 400


@pytest.mark.no_db
@pytest.mark.skipif(not WEB_DIST.exists(), reason="panel not built")
async def test_static_files_cannot_escape_the_panel_folder():
    app = create_app(run_background=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8710") as c:
        for path in ("/..%2F..%2Fpyproject.toml", "/%2e%2e/%2e%2e/.env", "/files/..%2F..%2Fpyproject.toml"):
            r = await c.get(path)
            assert r.status_code == 404, path
            assert "[project]" not in r.text
        assert (await c.get("/")).status_code == 200


async def test_run_again_copies_a_finished_task(client):
    created = (await client.post("/api/tasks", json={**TASK, "budget_usd": 0.7, "plan_review": "always"}, headers=PANEL)).json()
    r = await client.post(f"/api/tasks/{created['id']}/rerun", headers=PANEL)
    assert r.status_code == 409  # still queued, not finished
    from fleet import db

    p = await db.pool()
    await p.execute("update tasks set status = 'failed', error = 'out of credits' where id = $1", created["id"])
    r = await client.post(f"/api/tasks/{created['id']}/rerun", headers=PANEL)
    assert r.status_code == 201
    again = r.json()
    assert again["id"] != created["id"] and again["status"] == "queued"
    assert again["prompt"] == TASK["prompt"] and float(again["budget_usd"]) == 0.7 and again["plan_review"] == "always"


async def test_api_token_protects_api_and_screenshots(client, monkeypatch):
    from fleet.config import settings

    monkeypatch.setattr(settings(), "api_token", "s3cret-token")
    assert (await client.get("/api/tasks")).status_code == 401
    assert (await client.get("/files/screenshots/x/001.png")).status_code == 401
    assert (await client.get("/api/tasks", headers={"Authorization": "Bearer s3cret-token"})).status_code == 200
    assert (await client.get("/files/screenshots/x/001.png?token=s3cret-token")).status_code == 404  # allowed; file absent
