"""AdsPower client against a fake Local API (no AdsPower install needed)."""

import time

import httpx
import pytest

from fleet.adspower import AdsPower, AdsPowerError

pytestmark = pytest.mark.no_db

WS = "ws://127.0.0.1:61001/devtools/browser/abc"


def fake_adspower(state: dict):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.url.path, dict(request.url.params), time.monotonic()))
        uid = request.url.params.get("user_id")
        if request.url.path == "/api/v1/browser/active":
            data = {"status": "Active", "ws": {"puppeteer": WS}} if uid in state["open"] else {"status": "Inactive"}
            return httpx.Response(200, json={"code": 0, "data": data})
        if request.url.path == "/api/v1/browser/start":
            if uid == "broken":
                return httpx.Response(200, json={"code": -1, "msg": "Profile does not exist"})
            state["open"].add(uid)
            return httpx.Response(200, json={"code": 0, "data": {"ws": {"puppeteer": WS}, "debug_port": "61001"}})
        if request.url.path == "/api/v1/browser/stop":
            state["open"].discard(uid)
            return httpx.Response(200, json={"code": 0, "data": {}})
        if request.url.path == "/api/v1/user/list":
            page = int(request.url.params["page"])
            batch = [{"user_id": f"k{page}-{i}", "name": f"p{i}"} for i in range(100 if page == 1 else 7)]
            return httpx.Response(200, json={"code": 0, "data": {"list": batch}})
        return httpx.Response(404)

    return handler, calls


def client_for(handler) -> AdsPower:
    ap = AdsPower(base_url="http://adspower.test", api_key="")
    ap._client = httpx.AsyncClient(base_url="http://adspower.test", transport=httpx.MockTransport(handler))
    return ap


async def test_start_opens_then_reuses_open_browser():
    state = {"open": set()}
    handler, calls = fake_adspower(state)
    ap = client_for(handler)
    first = await ap.start("k1")
    second = await ap.start("k1")
    assert first.cdp_url == second.cdp_url == WS
    paths = [c[0] for c in calls]
    assert paths.count("/api/v1/browser/start") == 1  # second call found it already open


async def test_calls_are_spaced_for_the_rate_limit():
    handler, calls = fake_adspower({"open": set()})
    ap = client_for(handler)
    await ap.start("k1")
    await ap.stop("k1")
    gaps = [b[2] - a[2] for a, b in zip(calls, calls[1:], strict=False)]
    assert all(g >= 1.0 for g in gaps)


async def test_errors_surface_as_adspower_error():
    handler, _ = fake_adspower({"open": set()})
    ap = client_for(handler)
    with pytest.raises(AdsPowerError, match="does not exist"):
        await ap.start("broken")


async def test_list_profiles_pages_through_everything():
    handler, _ = fake_adspower({"open": set()})
    ap = client_for(handler)
    profiles = await ap.list_profiles()
    assert len(profiles) == 107
