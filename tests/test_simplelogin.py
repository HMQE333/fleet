"""SimpleLogin aliases (fleet/simplelogin.py): the request is what the API documents, the key never leaks, and nothing is
sent without a key. No network: the calls go through an httpx mock transport."""

import json
import re

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from fleet import simplelogin

pytestmark = pytest.mark.no_db

PANEL = {"X-Fleet-Client": "panel"}


@pytest.fixture
def answer(monkeypatch):
    def install(handler):
        monkeypatch.setattr(simplelogin, "_TRANSPORT", httpx.MockTransport(handler))
    return install


async def test_a_random_alias_is_asked_for_with_the_key_in_the_header(answer):
    seen = {}

    def handler(req):
        seen.update(url=str(req.url), auth=req.headers.get("Authentication"), body=json.loads(req.content), method=req.method)
        return httpx.Response(201, json={"email": "rybiapaka.k3f@simplelogin.com", "id": 1})

    answer(handler)
    email = await simplelogin.new_random_alias("KEY-123", note="sign-up for a shop", hostname="example.com")
    assert email == "rybiapaka.k3f@simplelogin.com"
    assert seen["method"] == "POST" and seen["auth"] == "KEY-123"
    assert seen["url"].startswith("https://app.simplelogin.io/api/alias/random/new?")
    assert "hostname=example.com" in seen["url"] and "mode=word" in seen["url"]
    assert seen["body"] == {"note": "sign-up for a shop"}


async def test_no_key_means_no_request_at_all(answer):
    def handler(req):
        raise AssertionError("a request must not be sent without a key")

    answer(handler)
    with pytest.raises(simplelogin.AliasError, match="set_key.ps1 SIMPLELOGIN_API_KEY"):
        await simplelogin.new_random_alias("")


async def test_a_rejected_key_is_reported_and_never_echoed(answer):
    answer(lambda req: httpx.Response(401, json={"error": "Invalid API key SECRETKEY9999"}))
    with pytest.raises(simplelogin.AliasError, match="rejected the API key") as e:
        await simplelogin.new_random_alias("SECRETKEY9999")
    assert "SECRETKEY9999" not in str(e.value)


async def test_other_refusals_keep_the_reason_without_the_key(answer):
    answer(lambda req: httpx.Response(403, json={"error": "Free plan reached the limit of aliases"}))
    with pytest.raises(simplelogin.AliasError) as e:
        await simplelogin.new_random_alias("KEY-123")
    assert re.search(r"HTTP 403\): Free plan reached the limit", str(e.value))


async def test_an_answer_without_an_address_is_an_error(answer):
    answer(lambda req: httpx.Response(201, json={"id": 1}))
    with pytest.raises(simplelogin.AliasError, match="without an alias address"):
        await simplelogin.new_random_alias("KEY-123")


async def test_the_note_is_capped_and_empty_notes_are_left_out(answer):
    seen = {}

    def handler(req):
        seen["body"] = json.loads(req.content)
        return httpx.Response(201, json={"email": "a@b.co"})

    answer(handler)
    await simplelogin.new_random_alias("K", note="x" * 500)
    assert len(seen["body"]["note"]) == simplelogin.NOTE_CHARS
    await simplelogin.new_random_alias("K", note="   ")
    assert seen["body"] == {}


async def test_the_api_route_returns_the_alias_and_maps_a_refusal_to_502(monkeypatch):
    from fleet.api import create_app

    async def ok(*, note="", hostname=""):
        return f"fresh+{hostname or 'any'}@simplelogin.com"

    monkeypatch.setattr(simplelogin, "create", ok)
    app = create_app(run_background=False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8710") as c:
        r = await c.post("/api/aliases/random", json={"hostname": "example.com"}, headers=PANEL)
        assert r.status_code == 201 and r.json() == {"alias": "fresh+example.com@simplelogin.com"}

        async def refused(*, note="", hostname=""):
            raise simplelogin.AliasError("SimpleLogin rejected the API key.")

        monkeypatch.setattr(simplelogin, "create", refused)
        r = await c.post("/api/aliases/random", json={}, headers=PANEL)
        assert r.status_code == 502 and "rejected" in r.json()["detail"]
        r = await c.post("/api/aliases/random", json={}, headers={})
        assert r.status_code == 403  # the panel header is required, like every change
