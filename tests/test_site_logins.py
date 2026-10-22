"""Saved logins: the store, the page-side login detection, and the full worker path with the Inbox.

The rules under test (RULES.md §3-4): the password goes from the encrypted store to the page and nowhere
else; a login is only typed into a login form; one attempt per run; the Inbox asks first unless the login
is marked automatic; "I'll do it myself" is always possible.
"""

import httpx
import pytest

from fleet import adspower, db, hitl, llm, openrouter, site_accounts, vault, worker
from fleet.api import create_app
from fleet.config import settings
from fleet.executors import browser_fast
from fleet.fastbrowser import decide, login, netguard
from fleet.fastbrowser.cdp import CDP
from fleet.fastbrowser.page import Page

from .conftest import finished, new_task, running_fleet, scripted, sub, subtasks, wait_for

PASSWORD = "correct horse battery"
USER = "alice@example.test"
PANEL = {"X-Fleet-Client": "panel"}


# -- the store ---------------------------------------------------------------------------------------


async def _profile(name: str = "Shop account") -> str:
    p = await db.pool()
    return await p.fetchval(
        "insert into profiles (adspower_id, name, enabled) values ($1, $2, true) returning id", f"id-{name}", name)


async def test_domains_are_cleaned_and_only_public_sites_are_accepted():
    assert site_accounts.normalize_domain("https://www.Example.com/login?x=1") == "example.com"
    assert site_accounts.normalize_domain("shop.example.co.uk:8443") == "shop.example.co.uk"
    for bad in ("localhost", "127.0.0.1", "printer.local", "", "just words"):
        with pytest.raises(site_accounts.SiteAccountError):
            site_accounts.normalize_domain(bad)


async def test_password_is_encrypted_at_rest_and_never_listed():
    account = await site_accounts.create("example.com", USER, PASSWORD)
    p = await db.pool()
    raw = await p.fetchrow("select * from site_accounts where id = $1", account["id"])
    assert PASSWORD.encode() not in bytes(raw["ciphertext"])
    assert all(PASSWORD not in str(v) for v in raw.values() if not isinstance(v, bytes))
    listed = await site_accounts.list_accounts()
    assert PASSWORD not in repr(listed) and not any("ciphertext" in row or "password" in row for row in listed)
    assert await site_accounts.credentials(account["id"]) == (USER, PASSWORD)
    await site_accounts.update(account["id"], password="a different password")
    assert (await site_accounts.credentials(account["id"]))[1] == "a different password"


async def test_redaction_relearns_a_saved_password_whenever_it_is_used_after_a_restart():
    account = await site_accounts.create("example.com", USER, PASSWORD)
    vault._seen_values.clear()  # a server restart: the in-memory list of values to scrub starts empty
    assert PASSWORD in vault.redact(f"oops {PASSWORD}")
    await site_accounts.credentials(account["id"])  # a login is about to be typed
    assert vault.redact(f"oops {PASSWORD}") == "oops [SITE_LOGIN_PASSWORD]"
    vault._seen_values.clear()
    p = await db.pool()
    await p.execute("update site_accounts set origin = 'fleet' where id = $1", account["id"])
    await site_accounts.reveal(account["id"])  # showing a generated password in the panel does the same
    assert vault.redact(f"oops {PASSWORD}") == "oops [SITE_LOGIN_PASSWORD]"


async def test_a_ciphertext_cannot_be_moved_to_another_login():
    a = await site_accounts.create("one.example.com", USER, PASSWORD)
    b = await site_accounts.create("two.example.com", "bob", "another secret")
    p = await db.pool()
    row_a = await p.fetchrow("select ciphertext, nonce from site_accounts where id = $1", a["id"])
    await p.execute("update site_accounts set ciphertext = $2, nonce = $3 where id = $1", b["id"], row_a["ciphertext"],
                    row_a["nonce"])
    with pytest.raises(site_accounts.SiteAccountError, match="can't be read"):
        await site_accounts.credentials(b["id"])


async def test_matching_prefers_the_profiles_own_login_then_the_most_specific_domain():
    mine = await _profile("Mine")
    await site_accounts.create("example.com", "global-user", "pw-global-1")
    await site_accounts.create("example.com", "mine-user", "pw-mine-1234", profile_id=mine)
    await site_accounts.create("shop.example.com", "shop-user", "pw-shop-1234")
    assert (await site_accounts.find_for_url("https://www.example.com/login", mine))["username"] == "mine-user"
    assert (await site_accounts.find_for_url("https://example.com/", None))["username"] == "global-user"
    assert (await site_accounts.find_for_url("https://shop.example.com/x", None))["username"] == "shop-user"
    assert await site_accounts.find_for_url("https://evil-example.com/", mine) is None
    assert await site_accounts.find_for_url("https://example.org/", mine) is None
    with pytest.raises(site_accounts.SiteAccountError, match="already a saved login"):
        await site_accounts.create("example.com", "again", "pw-again-1234")


# -- the API: write-only ----------------------------------------------------------------------------------


@pytest.fixture
async def client():
    app = create_app(run_background=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8710") as c:
        yield c


async def test_api_never_returns_a_password_and_never_echoes_a_rejected_one(client):
    body = {"domain": "https://www.Example.com/login", "username": USER, "password": PASSWORD, "auto_login": False}
    r = await client.post("/api/site-accounts", json=body, headers=PANEL)
    assert r.status_code == 201 and r.json()["domain"] == "example.com" and PASSWORD not in r.text
    assert (await client.post("/api/site-accounts", json=body)).status_code == 403  # CSRF guard
    listed = await client.get("/api/site-accounts")
    assert PASSWORD not in listed.text and listed.json()[0]["auto_login"] is False
    # an over-long password is rejected without being echoed back in the error
    too_long = {**body, "domain": "other.example.com", "password": "x" * 2000}
    r = await client.post("/api/site-accounts", json=too_long, headers=PANEL)
    assert r.status_code == 422 and "xxxxxxxx" not in r.text
    r = await client.patch(f"/api/site-accounts/{r.json() and listed.json()[0]['id']}", json={"auto_login": True},
                           headers=PANEL)
    assert r.status_code == 200 and r.json()["auto_login"] is True and PASSWORD not in r.text
    r = await client.post("/api/site-accounts", json={**body, "domain": "localhost"}, headers=PANEL)
    assert r.status_code == 400
    assert (await client.delete(f"/api/site-accounts/{listed.json()[0]['id']}", headers=PANEL)).status_code == 200


# -- the page side: login vs sign-up -----------------------------------------------------------------------


def _sender(page: Page):
    async def send(method, params=None):
        return await page.call(method, **(params or {}))

    return send


@pytest.mark.parametrize("path,kind", [
    ("signin.html", "login"), ("bare.html", "login"), ("signup.html", "register"),
    ("change_password.html", "register"), ("shop.html", "none"),
    ("login_and_signup.html", "login"),  # real run: Hacker News holds a login and a sign-up form on one page
])
async def test_only_login_forms_are_classified_as_login(browser_ws, site, path, kind):
    async with CDP(browser_ws) as cdp:
        page = await Page.open(cdp, f"{site}/{path}")
        try:
            assert (await login.classify(_sender(page)))["kind"] == kind
        finally:
            await page.close()


@pytest.mark.parametrize("path", ["signin.html", "bare.html"])
async def test_correct_login_is_typed_by_code_and_succeeds(browser_ws, site, path):
    async with CDP(browser_ws) as cdp:
        page = await Page.open(cdp, f"{site}/{path}")
        try:
            assert await login.perform(_sender(page), USER, PASSWORD) is True
            assert "Welcome back, alice" in await page.evaluate("document.body.innerText")
        finally:
            await page.close()


async def test_wrong_password_fails_once_and_never_touches_a_signup_form(browser_ws, site):
    async with CDP(browser_ws) as cdp:
        page = await Page.open(cdp, f"{site}/signin.html")
        try:
            assert await login.perform(_sender(page), USER, "wrong password", settle_seconds=1.5) is False
            assert "Wrong email or password" in await page.evaluate("document.body.innerText")
        finally:
            await page.close()
        for path in ("signup.html", "change_password.html"):
            page = await Page.open(cdp, f"{site}/{path}")
            try:
                assert await login.perform(_sender(page), USER, PASSWORD, settle_seconds=0.5) is False
                typed = await page.evaluate("[...document.querySelectorAll('input')].map(i => i.value).join('')")
                assert typed == ""  # nothing was typed into the form
            finally:
                await page.close()


async def test_a_login_form_next_to_a_signup_form_gets_the_login_and_the_signup_stays_empty(browser_ws, site):
    async with CDP(browser_ws) as cdp:
        page = await Page.open(cdp, f"{site}/login_and_signup.html")
        try:
            assert await login.perform(_sender(page), USER, "wrong password", settle_seconds=1.0) is False
            assert "Bad login." in await page.evaluate("document.body.innerText")  # the login form took it, once
            sign_up = await page.evaluate("[...document.querySelectorAll('#up input')].map(i => i.value).join('')")
            assert sign_up == "create account"  # only the button's own caption: nothing was typed into the sign-up form
        finally:
            await page.close()
        page = await Page.open(cdp, f"{site}/login_and_signup.html")
        try:
            assert await login.perform(_sender(page), USER, PASSWORD) is True
            assert "Welcome back, alice" in await page.evaluate("document.body.innerText")
        finally:
            await page.close()


# -- the whole worker: Inbox, automatic mode, hand-over ------------------------------------------------------


@pytest.fixture
async def env(fake, browser_ws, site, monkeypatch, tmp_path):
    """The real browser_fast worker; AdsPower is a headless Edge, Jev just says DONE once the page is past login."""
    p = await db.pool()
    profile_id = await p.fetchval(
        "insert into profiles (adspower_id, name, enabled, general_use) values ('k1', 'Scratch', true, true) returning id")
    monkeypatch.setitem(worker.EXECUTORS, "browser_fast", browser_fast.run)
    monkeypatch.setattr(settings(), "data_dir", tmp_path)
    monkeypatch.setattr(settings(), "close_browser_after_subtask", False)

    async def start(adspower_id, headless=False):
        return adspower.OpenBrowser(adspower_id, browser_ws, "0")

    monkeypatch.setattr(adspower.client(), "start", start)

    async def key():
        return "sk-or-test"

    async def local_is_fine(url):
        return None

    async def answer(**kw):
        return "Logged in and done."

    monkeypatch.setattr(openrouter, "api_key", key)
    monkeypatch.setattr(netguard, "check_url_resolved", local_is_fine)
    monkeypatch.setattr(decide, "choose", scripted(["DONE"], []))
    monkeypatch.setattr(llm, "complete", answer)

    async def save(password: str = PASSWORD, *, auto: bool):
        account_id = await p.fetchval("select gen_random_uuid()")
        ciphertext, nonce, key_id = vault.seal(f"site-login:{account_id}", password)
        await p.execute(
            "insert into site_accounts (id, domain, username, ciphertext, nonce, key_id, auto_login) "
            "values ($1, '127.0.0.1', $2, $3, $4, $5, $6)", account_id, USER, ciphertext, nonce, key_id, auto)
        return account_id

    async def run(page: str):
        fake.plan.subtasks = [sub("b1", kind="browser_fast", url=f"{site}/{page}")]
        return await new_task()

    async def card():
        return await wait_for(lambda: p.fetchrow("select * from subtasks where status = 'needs_human'"), timeout=30)

    async def nothing_leaked(password: str):
        rows = await p.fetch("select row_to_json(e)::text t from events e union all select row_to_json(s)::text "
                             "from subtasks s union all select row_to_json(t)::text from tasks t")
        assert rows and not any(password in r["t"] for r in rows), "a password reached the database"

    return type("Env", (), {"p": p, "save": staticmethod(save), "run": staticmethod(run), "card": staticmethod(card),
                            "nothing_leaked": staticmethod(nothing_leaked), "profile_id": profile_id})


async def test_automatic_login_runs_without_asking_and_leaks_nothing(env):
    account_id = await env.save(auto=True)
    task = await env.run("signin.html")
    async with running_fleet():
        t = await wait_for(lambda: finished(task["id"]), timeout=40)
    b1 = (await subtasks(task["id"]))["b1"]
    assert t["status"] == "done" and b1["status"] == "done", b1["error"]
    login_events = await env.p.fetch("select message from events where type = 'login'")
    assert [e["message"] for e in login_events] == [f"Logged in as {USER} on 127.0.0.1 with the saved login (automatic)"]
    assert await env.p.fetchval("select last_used_at is not null from site_accounts where id = $1", account_id)
    assert b1["trace"] in (None, [])  # logging in is state, not part of a replayable path
    await env.nothing_leaked(PASSWORD)


async def test_by_default_the_inbox_asks_and_approval_logs_in(env):
    await env.save(auto=False)
    task = await env.run("signin.html")
    async with running_fleet():
        card = await env.card()
        assert card["human_request"]["kind"] == "login_saved" and USER in card["human_request"]["message"]
        assert await env.p.fetchval("select last_used_at is null from site_accounts")  # nothing typed yet
        assert await hitl.respond(card["id"], "approve")
        t = await wait_for(lambda: finished(task["id"]), timeout=40)
    assert t["status"] == "done"
    await env.nothing_leaked(PASSWORD)


async def test_i_will_do_it_myself_types_nothing(env):
    await env.save(auto=False)
    task = await env.run("signin.html")
    async with running_fleet():
        first = await env.card()
        assert first["human_request"]["kind"] == "login_saved"
        assert await hitl.respond(first["id"], "done")  # "I'll do it myself"
        second = await wait_for(lambda: env.p.fetchrow(
            "select * from subtasks where status = 'needs_human' and human_request->>'kind' = 'login'"), timeout=30)
        assert "asks for a login" in second["human_request"]["message"]  # the usual hand-off card
        assert await hitl.respond(second["id"], "reject")
        await wait_for(lambda: finished(task["id"]), timeout=40)
    assert await env.p.fetchval("select last_used_at is null from site_accounts")  # the password was never read


async def test_wrong_saved_password_is_tried_once_then_handed_to_the_operator(env):
    account_id = await env.save("not the right one", auto=True)
    task = await env.run("signin.html")
    async with running_fleet():
        card = await wait_for(lambda: env.p.fetchrow(
            "select * from subtasks where status = 'needs_human' and human_request->>'kind' = 'login'"), timeout=40)
        assert "didn't work" in card["human_request"]["message"]
        assert await hitl.respond(card["id"], "reject")
        await wait_for(lambda: finished(task["id"]), timeout=40)
    warnings = await env.p.fetch("select message from events where type = 'login' and level = 'warn'")
    assert len(warnings) == 1  # one attempt, not a lockout loop
    assert await env.p.fetchval("select last_failed_at is not null from site_accounts where id = $1", account_id)
    await env.nothing_leaked("not the right one")


async def test_a_signup_page_is_never_filled_even_in_automatic_mode(env):
    await env.save(auto=True)
    task = await env.run("signup.html")
    async with running_fleet():
        card = await env.card()
        assert card["human_request"]["kind"] == "login"  # the plain hand-off, no saved-login card
        assert await hitl.respond(card["id"], "reject")
        await wait_for(lambda: finished(task["id"]), timeout=40)
    assert await env.p.fetchval("select last_used_at is null from site_accounts")
