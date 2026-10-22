"""Registration: Fleet creates an account only when the task allows it, the operator approves, and the form is
one it fully understands. Covers the page side (what is typed, what never is), the Inbox flow, and the limits.

The rules under test (RULES.md §1, §3-4): off by default; the operator's own details; every sign-up approved;
terms ticked only as the card said, newsletter never; one account per site and profile plus a daily limit; the
password saved before submitting and removed if the site refused; captchas/emails stay with the operator.
"""

import httpx
import pytest

from fleet import adspower, db, hitl, identity, llm, openrouter, prompts, registration, site_accounts, worker
from fleet.api import create_app
from fleet.config import settings
from fleet.executors import browser_fast
from fleet.fastbrowser import decide, netguard, register
from fleet.fastbrowser.cdp import CDP
from fleet.fastbrowser.page import Page
from fleet.planner import _task_block

from .conftest import finished, new_task, running_fleet, scripted, sub, subtasks, wait_for

PASSWORD = "Fixed-Pass-1234!xyz"
ME = {"first_name": "Alice", "last_name": "Example", "email": "alice@example.test", "username": "alice"}
PANEL = {"X-Fleet-Client": "panel"}
NO_ONE = {"first_name": "", "last_name": "", "email": "", "username": ""}
FAKE_ROW = "insert into site_accounts (domain, username, ciphertext, nonce, key_id, origin) values ($1, 'x', $2, $2, 'k', $3)"


def _sender(page: Page):
    async def send(method, params=None):
        return await page.call(method, **(params or {}))

    return send


def _values(password: str = PASSWORD) -> dict[str, str]:
    return {"first_name": "Alice", "last_name": "Example", "full_name": "Alice Example", "email": ME["email"],
            "email_confirm": ME["email"], "username": "alice", "password": password, "confirm": password}


async def _page_text(page: Page) -> str:
    return await page.evaluate("document.body.innerText")


# -- the page side: what is a sign-up form, what is typed ---------------------------------------------------------


async def test_the_sign_up_form_is_read_field_by_field(browser_ws, site):
    async with CDP(browser_ws) as cdp:
        page = await Page.open(cdp, f"{site}/signup_full.html")
        try:
            info = await register.plan(_sender(page))
        finally:
            await page.close()
    assert info["kind"] == "register" and info["others"] == [] and info["submit"]
    assert set(info["roles"]) == {"email", "username", "password", "confirm"}
    assert info["terms"] == ["I agree to the Terms of Service and Privacy Policy"]  # the newsletter box isn't listed


@pytest.mark.parametrize("path,kind,others", [
    ("signup_phone.html", "register", ["Phone number"]),  # a required field Fleet has no honest value for
    ("change_password.html", "change", None),  # never a form that changes a password
    ("shop.html", "none", None),
    ("signup_cross.html", "register", []),
])
async def test_forms_fleet_does_not_understand_are_flagged(browser_ws, site, path, kind, others):
    async with CDP(browser_ws) as cdp:
        page = await Page.open(cdp, f"{site}/{path}")
        try:
            info = await register.plan(_sender(page))
        finally:
            await page.close()
    assert info["kind"] == kind
    if others is not None:
        assert info["others"] == others


async def test_a_page_holding_a_login_form_is_never_planned_or_filled_as_a_sign_up(browser_ws, site):
    """news.ycombinator.com/login holds a login form and a sign-up form. login.classify rightly calls it a login page (so a
    saved login works there); the sign-up planner must agree and refuse, so the Inbox never promises a sign-up that fill() would
    then silently skip, and nothing is typed into either form."""
    async with CDP(browser_ws) as cdp:
        page = await Page.open(cdp, f"{site}/login_and_signup.html")
        try:
            info = await register.plan(_sender(page))
            result = await register.fill(_sender(page), _values(), settle_seconds=0.5)
            typed = await page.evaluate("[...document.querySelectorAll('input[type=text],input[type=password]')].map(i => i.value).join('')")
        finally:
            await page.close()
    assert info["kind"] != "register"
    assert not result.accepted and not result.typed
    assert typed == ""


async def test_a_form_that_posts_to_another_site_is_flagged(browser_ws, site):
    async with CDP(browser_ws) as cdp:
        results = {}
        for path in ("signup_cross.html", "signup_full.html"):
            page = await Page.open(cdp, f"{site}/{path}")
            try:
                results[path] = await register.plan(_sender(page))
            finally:
                await page.close()
    assert results["signup_cross.html"]["crossSite"] is True
    assert results["signup_cross.html"]["sendsTo"] == "collector.example.net"
    assert results["signup_full.html"]["crossSite"] is False


async def test_fill_types_exactly_the_given_values_ticks_terms_and_never_the_newsletter(browser_ws, site):
    async with CDP(browser_ws) as cdp:
        page = await Page.open(cdp, f"{site}/signup_full.html?mode=ok")
        try:
            result = await register.fill(_sender(page), _values(), settle_seconds=5)
            text = await _page_text(page)
        finally:
            await page.close()
    assert result.accepted and not result.needs_verification
    assert result.typed == ["email", "username", "password", "confirm"]
    assert "Welcome, alice!" in text
    assert f"email={ME['email']} username=alice pw={PASSWORD} confirm-match=true terms=true newsletter=false" in text


async def test_a_page_that_says_check_your_email_is_reported_as_needing_verification(browser_ws, site):
    async with CDP(browser_ws) as cdp:
        page = await Page.open(cdp, f"{site}/signup_full.html?mode=verify")
        try:
            result = await register.fill(_sender(page), _values(), settle_seconds=5)
        finally:
            await page.close()
    assert result.accepted and result.needs_verification


async def test_a_refused_form_is_not_reported_as_accepted(browser_ws, site):
    async with CDP(browser_ws) as cdp:
        page = await Page.open(cdp, f"{site}/signup_full.html?mode=reject")
        try:
            result = await register.fill(_sender(page), _values(), settle_seconds=1.5)
            assert "taken" in await _page_text(page)
        finally:
            await page.close()
    assert not result.accepted and result.submitted  # sent, and the page still shows the form


@pytest.mark.parametrize("path", ["signup_phone.html", "signup_cross.html", "change_password.html", "signin.html",
                                  "bare.html", "shop.html"])
async def test_fill_types_nothing_into_forms_it_does_not_fully_understand(browser_ws, site, path):
    async with CDP(browser_ws) as cdp:
        page = await Page.open(cdp, f"{site}/{path}")
        try:
            result = await register.fill(_sender(page), _values(), settle_seconds=0.5)
            typed = await page.evaluate("[...document.querySelectorAll('input')].map(i => i.value).join('')")
        finally:
            await page.close()
    assert not result.accepted and not result.submitted and result.typed == [] and typed == ""


def test_generated_passwords_are_long_random_and_use_every_character_class():
    seen = {registration.generate_password() for _ in range(20)}
    assert len(seen) == 20
    for pw in seen:
        assert len(pw) == 20
        assert any(c.islower() for c in pw) and any(c.isupper() for c in pw)
        assert any(c.isdigit() for c in pw) and any(not c.isalnum() for c in pw)


# -- the operator's details, the API -------------------------------------------------------------------------------


async def test_identity_is_validated_and_round_trips():
    assert await identity.get() == NO_ONE
    assert not identity.complete(await identity.get())
    assert await identity.put(ME) == ME and await identity.get() == ME and identity.complete(ME)
    with pytest.raises(identity.IdentityError):
        await identity.put({**ME, "email": "not an email"})
    with pytest.raises(identity.IdentityError):
        await identity.put({**ME, "username": "a b"})
    assert identity.username_for({"email": "bob.smith@example.test", "username": ""}) == "bob.smith"


@pytest.fixture
async def client():
    app = create_app(run_background=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8710") as c:
        yield c


async def test_api_identity_and_task_option_and_reveal(client):
    assert (await client.put("/api/settings/identity", json=ME)).status_code == 403  # CSRF guard
    r = await client.put("/api/settings/identity", json=ME, headers=PANEL)
    assert r.status_code == 200 and (await client.get("/api/settings/identity")).json() == ME
    assert (await client.put("/api/settings/identity", json={**ME, "email": "nope"}, headers=PANEL)).status_code == 400

    r = await client.post("/api/tasks", json={"prompt": "sign me up somewhere", "allow_registration": True}, headers=PANEL)
    assert r.status_code in (200, 201) and r.json()["allow_registration"] is True
    r = await client.post("/api/tasks", json={"prompt": "just read"}, headers=PANEL)
    assert r.json()["allow_registration"] is False  # off unless asked for

    mine = await site_accounts.create("example.com", "me", "my-own-password-1", origin="operator")
    made = await site_accounts.create("example.org", "alice", PASSWORD, origin="fleet", label=registration.DONE)
    assert (await client.post(f"/api/site-accounts/{made['id']}/reveal")).status_code == 403  # CSRF guard
    r = await client.post(f"/api/site-accounts/{mine['id']}/reveal", headers=PANEL)
    assert r.status_code == 400 and "my-own-password-1" not in r.text  # what the operator typed in stays write-only
    r = await client.post(f"/api/site-accounts/{made['id']}/reveal", headers=PANEL)
    assert r.status_code == 200 and r.json() == {"password": PASSWORD} and r.headers["cache-control"] == "no-store"
    p = await db.pool()
    shown = await p.fetch("select message, data::text d from events where type = 'site_login.revealed'")
    assert len(shown) == 1 and "example.org" in shown[0]["message"]
    assert PASSWORD not in shown[0]["message"] + str(shown[0]["d"])  # the audit entry never holds the password
    assert PASSWORD not in (await client.get("/api/site-accounts")).text


def test_the_planner_and_the_browser_agent_are_told_what_they_may_do_about_accounts():
    task = {"prompt": "x", "allowed_domains": [], "budget_usd": 1, "cost_usd": 0, "require_approval": True}
    assert "NOT allowed" in _task_block({**task, "allow_registration": False})
    assert "ALLOWED" in _task_block({**task, "allow_registration": True})
    assert "Don't create accounts" in prompts.browser_rules(False)
    assert "STOP at the form" in prompts.browser_rules(True)
    assert "Never type into a sign-up form" in prompts.browser_rules(True)


# -- the whole worker: Inbox, limits, hand-over -------------------------------------------------------------------


@pytest.fixture
async def env(fake, browser_ws, site, monkeypatch, tmp_path):
    """The real browser_fast worker over a headless Edge; Jev says DONE once the page is past the sign-up form."""
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
        return "Done."

    monkeypatch.setattr(openrouter, "api_key", key)
    monkeypatch.setattr(netguard, "check_url_resolved", local_is_fine)
    monkeypatch.setattr(decide, "choose", scripted(["DONE"], []))
    monkeypatch.setattr(llm, "complete", answer)
    monkeypatch.setattr(registration, "generate_password", lambda length=20: PASSWORD)
    monkeypatch.setattr(site_accounts, "normalize_domain", lambda v: "127.0.0.1")  # the test site is local
    await identity.put(ME)

    async def run(page: str, *, allow: bool = True):
        fake.plan.subtasks = [sub("b1", kind="browser_fast", url=f"{site}/{page}")]
        task = await new_task()
        await p.execute("update tasks set allow_registration = $2 where id = $1", task["id"], allow)
        return task

    async def card(kind: str | None = None):
        where = "status = 'needs_human'" + (f" and human_request->>'kind' = '{kind}'" if kind else "")
        return await wait_for(lambda: p.fetchrow(f"select * from subtasks where {where}"), timeout=40)

    async def accounts():
        return await p.fetch("select * from site_accounts order by created_at")

    async def asked_to_register():
        return await p.fetchval("select count(*) from events where type = 'human.request' and data->>'kind' = 'register'")

    async def nothing_leaked(password: str = PASSWORD):
        rows = await p.fetch("select row_to_json(e)::text t from events e union all select row_to_json(s)::text "
                             "from subtasks s union all select row_to_json(t)::text from tasks t")
        assert rows and not any(password in r["t"] for r in rows), "a password reached the database"

    return type("Env", (), {"p": p, "run": staticmethod(run), "card": staticmethod(card),
                            "accounts": staticmethod(accounts), "asked": staticmethod(asked_to_register),
                            "nothing_leaked": staticmethod(nothing_leaked), "profile_id": profile_id})


async def test_an_approved_sign_up_creates_the_account_and_saves_the_password(env):
    task = await env.run("signup_full.html?mode=ok")
    async with running_fleet():
        card = await env.card("register")
        message = card["human_request"]["message"]
        assert "127.0.0.1 as alice" in message and "email" in message and "Terms of Service" in message
        assert "newsletter" not in message.lower()
        assert await env.accounts() == []  # nothing happens before the operator says yes
        assert await hitl.respond(card["id"], "approve")
        t = await wait_for(lambda: finished(task["id"]), timeout=40)
    b1 = (await subtasks(task["id"]))["b1"]
    assert t["status"] == "done" and b1["status"] == "done", b1["error"]
    (account,) = await env.accounts()
    assert (account["username"], account["origin"], account["label"], account["auto_login"]) == (
        "alice", "fleet", registration.DONE, False)
    assert await site_accounts.reveal(account["id"]) == ("127.0.0.1", PASSWORD)
    assert b1["side_effects"] is True  # an account exists now: this step must not be retried automatically
    assert await env.p.fetchval("select count(*) from events where type = 'register'") == 1
    await env.nothing_leaked()


async def test_a_confirmation_email_goes_to_the_operator(env):
    task = await env.run("signup_full.html?mode=verify")
    async with running_fleet():
        assert await hitl.respond((await env.card("register"))["id"], "approve")
        verify = await env.card("verify_email")
        assert ME["email"] in verify["human_request"]["message"]
        assert await hitl.respond(verify["id"], "done")
        t = await wait_for(lambda: finished(task["id"]), timeout=40)
    assert t["status"] == "done" and len(await env.accounts()) == 1


async def test_i_will_do_it_myself_types_nothing_and_saves_nothing(env):
    task = await env.run("signup_full.html")
    async with running_fleet():
        assert await hitl.respond((await env.card("register"))["id"], "done")
        hand_off = await env.card("login")  # the usual hand-off: the operator signs up in the AdsPower window
        assert "asks for a login" in hand_off["human_request"]["message"]
        assert await hitl.respond(hand_off["id"], "reject")
        await wait_for(lambda: finished(task["id"]), timeout=40)
    assert await env.accounts() == []


async def test_rejecting_the_card_stops_the_step_without_an_account(env):
    task = await env.run("signup_full.html")
    async with running_fleet():
        assert await hitl.respond((await env.card("register"))["id"], "reject")
        await wait_for(lambda: finished(task["id"]), timeout=40)
    b1 = (await subtasks(task["id"]))["b1"]
    assert b1["status"] == "failed" and "operator stopped" in b1["error"] and await env.accounts() == []


async def test_a_form_that_was_sent_but_not_confirmed_keeps_the_password(env):
    """The page still shows the form, but it was submitted: the account may exist, so the password is never thrown away."""
    task = await env.run("signup_full.html?mode=reject")
    async with running_fleet():
        assert await hitl.respond((await env.card("register"))["id"], "approve")
        hand_off = await env.card("login")
        assert "can't tell whether the account exists" in hand_off["human_request"]["message"]
        assert await hitl.respond(hand_off["id"], "reject")
        await wait_for(lambda: finished(task["id"]), timeout=40)
    (account,) = await env.accounts()
    assert account["label"] == registration.UNCONFIRMED
    assert await site_accounts.reveal(account["id"]) == ("127.0.0.1", PASSWORD)
    await env.nothing_leaked()


async def test_nothing_submitted_means_no_account_row(env, monkeypatch):
    async def could_not_fill(send, values, **kw):
        return register.FillResult(False)

    monkeypatch.setattr(register, "fill", could_not_fill)
    task = await env.run("signup_full.html")
    async with running_fleet():
        assert await hitl.respond((await env.card("register"))["id"], "approve")
        hand_off = await env.card("login")
        assert "nothing was submitted" in hand_off["human_request"]["message"]
        assert await hitl.respond(hand_off["id"], "reject")
        await wait_for(lambda: finished(task["id"]), timeout=40)
    assert await env.accounts() == []


async def test_a_login_that_appears_while_the_operator_decides_stops_the_sign_up(env, monkeypatch):
    real_ask = hitl.ask

    async def ask(subtask, kind, message, *, url=None):
        response = await real_ask(subtask, kind, message, url=url)
        if kind == "register":  # meanwhile another step signed up (or the operator saved a login)
            await env.p.execute(FAKE_ROW, "127.0.0.1", b"\x00", "operator")
        return response

    monkeypatch.setattr(hitl, "ask", ask)
    task = await env.run("signup_full.html")
    async with running_fleet():
        assert await hitl.respond((await env.card("register"))["id"], "approve")
        hand_off = await env.card("login")
        assert "already have a saved login" in hand_off["human_request"]["message"]
        assert await hitl.respond(hand_off["id"], "reject")
        await wait_for(lambda: finished(task["id"]), timeout=40)
    assert len(await env.accounts()) == 1  # only the one that was already there


async def test_a_task_that_does_not_allow_registration_gets_the_plain_hand_off(env):
    task = await env.run("signup_full.html", allow=False)
    async with running_fleet():
        card = await env.card()
        assert card["human_request"]["kind"] == "login" and "asks for a login" in card["human_request"]["message"]
        assert await hitl.respond(card["id"], "reject")
        await wait_for(lambda: finished(task["id"]), timeout=40)
    assert await env.accounts() == [] and await env.asked() == 0


async def _refused(env, page: str, expect: str):
    """A sign-up Fleet declines: the operator gets a hand-off card saying why, and is never asked to approve."""
    task = await env.run(page)
    async with running_fleet():
        card = await env.card("login")
        assert expect in card["human_request"]["message"]
        assert await hitl.respond(card["id"], "reject")
        await wait_for(lambda: finished(task["id"]), timeout=40)
    assert await env.asked() == 0


async def test_an_existing_login_for_the_site_means_no_second_account(env):
    await env.p.execute(FAKE_ROW, "127.0.0.1", b"\x00", "operator")
    await _refused(env, "signup_full.html", "already have a saved login")
    assert len(await env.accounts()) == 1


async def test_the_daily_limit_stops_another_account(env, monkeypatch):
    monkeypatch.setattr(settings(), "registrations_per_day", 1)
    await env.p.execute(FAKE_ROW, "other.example.com", b"\x00", "fleet")
    await _refused(env, "signup_full.html", "daily limit")
    assert len(await env.accounts()) == 1


async def test_without_the_operators_email_fleet_does_not_sign_up(env):
    await identity.put(NO_ONE)
    await _refused(env, "signup_full.html", "needs your email address")
    assert await env.accounts() == []


async def test_a_form_that_wants_something_fleet_does_not_have_is_left_to_the_operator(env):
    await _refused(env, "signup_phone.html", "Phone number")
    assert await env.accounts() == []


async def test_a_form_that_posts_to_another_site_is_left_to_the_operator(env):
    await _refused(env, "signup_cross.html", "sends your details to another site")
    assert await env.accounts() == []


async def test_a_change_password_page_is_never_treated_as_a_sign_up(env):
    task = await env.run("change_password.html")
    async with running_fleet():
        card = await env.card("login")
        assert "asks for a login" in card["human_request"]["message"]
        assert await hitl.respond(card["id"], "reject")
        await wait_for(lambda: finished(task["id"]), timeout=40)
    assert await env.accounts() == [] and await env.asked() == 0
