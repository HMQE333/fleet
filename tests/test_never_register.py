"""Sites Fleet never signs up on: a listed site (and its subdomains, never look-alikes) is refused before the operator is even asked,
whatever the task allows.
"""

import httpx
import pytest

from fleet import hitl, identity, site_accounts
from fleet.api import create_app

from .conftest import finished, running_fleet, wait_for
from .test_registration import _refused, env  # noqa: F401  (the real worker on a sign-up page)

PANEL = {"X-Fleet-Client": "panel"}


@pytest.fixture
async def client():
    app = create_app(run_background=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8710") as c:
        yield c


# -- sites Fleet never signs up on --------------------------------------------------------------------------------------------


async def test_listed_sites_cover_their_subdomains_but_never_look_alikes():
    assert await identity.block("https://www.Shop.Example.com/signup?x=1") == "shop.example.com"
    assert await identity.block("shop.example.com") == "shop.example.com"  # the same entry once
    assert await identity.blocked_domains() == ["shop.example.com"]
    for host in ("shop.example.com", "www.shop.example.com", "eu.shop.example.com", "SHOP.example.com"):
        assert await identity.is_blocked(host), host
    for host in ("example.com", "evil-shop.example.com", "notshop.example.com", "shop.example.com.evil.test", "", None):
        assert not await identity.is_blocked(host), host
    for bad in ("localhost", "127.0.0.1", "printer.local", "just words", ""):
        with pytest.raises(site_accounts.SiteAccountError):
            await identity.block(bad)
    assert await identity.unblock("shop.example.com") and not await identity.unblock("shop.example.com")
    assert not await identity.is_blocked("shop.example.com")


async def test_the_api_lists_adds_and_removes_listed_sites(client):
    assert (await client.get("/api/settings/no-register")).json() == []
    assert (await client.post("/api/settings/no-register", json={"domain": "example.com"})).status_code == 403  # CSRF guard
    r = await client.post("/api/settings/no-register", json={"domain": "https://Example.com/x"}, headers=PANEL)
    assert r.status_code == 200 and r.json() == ["example.com"]
    assert (await client.post("/api/settings/no-register", json={"domain": "localhost"}, headers=PANEL)).status_code == 400
    assert (await client.delete("/api/settings/no-register/example.com")).status_code == 403
    assert (await client.delete("/api/settings/no-register/nope.com", headers=PANEL)).status_code == 404
    assert (await client.delete("/api/settings/no-register/example.com", headers=PANEL)).json() == []


async def test_a_listed_site_is_refused_before_the_operator_is_asked_and_allowed_again_after_removal(env):  # noqa: F811
    await identity.block("127.0.0.1")  # the test site (the fixture maps every address to this domain)
    await _refused(env, "signup_full.html", "never to sign up on 127.0.0.1")
    assert await env.accounts() == []
    assert await identity.unblock("127.0.0.1")
    task = await env.run("signup_full.html")
    async with running_fleet():
        card = await env.card("register")  # the normal approval card is back
        assert await hitl.respond(card["id"], "reject")
        await wait_for(lambda: finished(task["id"]), timeout=40)
