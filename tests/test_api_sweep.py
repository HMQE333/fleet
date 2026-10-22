"""A sweep over EVERY route of the API, so a route added later can't quietly skip the guards.

 - changes (POST / PUT / PATCH / DELETE) must carry the panel header, from a browser page of the same site only;
 - when an access token is set, every route refuses a request without it (including the screenshots under /files);
 - nothing is served outside /api except the panel itself (no generated docs or schema pages).
"""

import re

import httpx
import pytest
from fastapi.routing import APIRoute

from fleet.api import UNSAFE_METHODS, create_app
from fleet.config import settings

pytestmark = pytest.mark.no_db

PANEL = {"X-Fleet-Client": "panel"}
UUID = "00000000-0000-0000-0000-000000000000"


@pytest.fixture
def app():
    return create_app(run_background=False)


@pytest.fixture
async def client(app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8710") as c:
        yield c


def _routes(app) -> list[tuple[str, str]]:
    """(method, concrete path) for every API route: path parameters are filled in with placeholders."""
    found = []
    for route in app.routes:
        if isinstance(route, APIRoute):
            path = re.sub(r"\{[^}]*(id|key)[^}]*\}", UUID, route.path)
            path = re.sub(r"\{[^}]*\}", "x", path)
            found.extend((method, path) for method in sorted(route.methods - {"HEAD", "OPTIONS"}))
    return found


def test_the_sweep_really_sees_the_whole_api(app):
    routes = _routes(app)
    assert len(routes) >= 45 and any(m in UNSAFE_METHODS for m, _ in routes)
    assert {"GET", "POST", "PUT", "PATCH", "DELETE"} <= {m for m, _ in routes}


def test_every_api_route_lives_under_api_and_no_generated_docs_are_served(app):
    outside = [(m, p) for m, p in _routes(app) if not p.startswith("/api/")]
    assert outside == [], f"routes outside /api would skip the token guard's habits: {outside}"
    assert app.docs_url is None and app.redoc_url is None and app.openapi_url is None


async def test_the_generated_docs_and_schema_are_not_served(client):
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert (await client.get(path)).status_code in (404, 405), path


async def test_every_change_without_the_panel_header_is_refused(app, client):
    unsafe = [(m, p) for m, p in _routes(app) if m in UNSAFE_METHODS]
    assert len(unsafe) >= 25
    for method, path in unsafe:
        r = await client.request(method, path, json={})
        assert r.status_code == 403, f"{method} {path} answered {r.status_code} without the panel header"


async def test_every_change_from_another_site_is_refused_even_with_the_header(app, client):
    for method, path in [(m, p) for m, p in _routes(app) if m in UNSAFE_METHODS]:
        r = await client.request(method, path, json={}, headers={**PANEL, "Origin": "https://evil.example"})
        assert r.status_code == 403, f"{method} {path} answered {r.status_code} to a request from another site"


async def test_with_an_access_token_every_route_refuses_requests_without_it(app, client, monkeypatch):
    monkeypatch.setattr(settings(), "api_token", "s3cret")
    for method, path in _routes(app):
        for headers in ({}, {"Authorization": "Bearer wrong"}):
            r = await client.request(method, path, json={}, headers={**PANEL, **headers})
            assert r.status_code == 401, f"{method} {path} answered {r.status_code} without the right token"
        r = await client.request(method, path, json={}, params={"token": "wrong"}, headers=PANEL)
        assert r.status_code == 401, f"{method} {path} accepted a wrong ?token="


async def test_screenshots_and_files_need_the_token_too(client, monkeypatch):
    monkeypatch.setattr(settings(), "api_token", "s3cret")
    assert (await client.get("/files/screenshots/x/001.jpg")).status_code == 401
    assert (await client.get("/files/screenshots/x/001.jpg", params={"token": "wrong"})).status_code == 401
