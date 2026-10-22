"""Two-step logins: a username page first, the password page after (one page that swaps, or two pages).

The rules under test (RULES.md §3-4): Fleet only looks for a username step when the operator saved a login for the
site; it never types into a newsletter box, contact form or sign-up page; the Inbox asks first unless the login is
automatic; one attempt per run; the password is typed by code and leaks nowhere.
"""

import pytest

from fleet import hitl
from fleet.executors import RunContext
from fleet.fastbrowser import login
from fleet.fastbrowser.cdp import CDP
from fleet.fastbrowser.page import Page
from fleet.saved_login import SavedLogin

from .conftest import finished, running_fleet, subtasks, wait_for
from .test_site_logins import PASSWORD, USER, _sender, env  # noqa: F401  (the real-worker fixture is shared)


# -- the page side: what is a username step ---------------------------------------------------------------------


@pytest.mark.parametrize("path,kind", [
    ("signin_step.html", "login_step"), ("signin_step_pages.html", "login_step"),
    ("newsletter.html", "none"), ("contact.html", "none"), ("signup_step.html", "none"), ("comment_signin.html", "none"),
    ("shop.html", "none"), ("signin.html", "none"), ("bare.html", "none"),  # these have a password field or no login at all
])
async def test_only_a_username_page_that_talks_about_signing_in_counts_as_a_step(browser_ws, site, path, kind):
    async with CDP(browser_ws) as cdp:
        page = await Page.open(cdp, f"{site}/{path}")
        try:
            assert (await login.classify_step(_sender(page)))["kind"] == kind
        finally:
            await page.close()


async def test_a_username_page_is_not_a_login_form_for_the_one_page_login_code(browser_ws, site):
    async with CDP(browser_ws) as cdp:
        page = await Page.open(cdp, f"{site}/signin_step.html")
        try:
            assert (await login.classify(_sender(page)))["kind"] == "none"
            assert await login.perform(_sender(page), USER, PASSWORD) is False  # no password field: refuses
        finally:
            await page.close()


@pytest.mark.parametrize("path", ["signin_step.html", "signin_step_pages.html"])
async def test_both_steps_are_typed_by_code_and_succeed(browser_ws, site, path):
    async with CDP(browser_ws) as cdp:
        page = await Page.open(cdp, f"{site}/{path}")
        try:
            assert await login.perform_two_step(_sender(page), USER, PASSWORD) is True
            assert "Welcome back, alice" in await page.evaluate("document.body.innerText")
        finally:
            await page.close()


@pytest.mark.parametrize("path", ["signin_step.html", "signin_step_pages.html"])
async def test_a_wrong_password_fails_after_the_username_step_without_retrying(browser_ws, site, path):
    async with CDP(browser_ws) as cdp:
        page = await Page.open(cdp, f"{site}/{path}")
        try:
            assert await login.perform_two_step(_sender(page), USER, "wrong password", settle_seconds=1.5) is False
            assert "Wrong password" in await page.evaluate("document.body.innerText")
        finally:
            await page.close()


async def test_an_unknown_username_stops_at_the_first_step(browser_ws, site):
    async with CDP(browser_ws) as cdp:
        page = await Page.open(cdp, f"{site}/signin_step.html")
        try:
            assert await login.perform_two_step(_sender(page), "nobody@example.test", PASSWORD, settle_seconds=1.0) is False
            text = await page.evaluate("document.body.innerText")
            assert "Couldn't find your account" in text
            assert await page.evaluate("document.querySelectorAll('input[type=password]').length") == 0  # never got to it
        finally:
            await page.close()


@pytest.mark.parametrize("path", ["newsletter.html", "contact.html", "signup_step.html", "comment_signin.html", "shop.html", "signin.html"])
async def test_nothing_is_typed_into_pages_that_are_not_a_username_step(browser_ws, site, path):
    async with CDP(browser_ws) as cdp:
        page = await Page.open(cdp, f"{site}/{path}")
        try:
            assert await login.perform_step(_sender(page), USER, settle_seconds=0.5) is False
            typed = await page.evaluate("[...document.querySelectorAll('input')].map(i => i.value).join('')")
            assert typed == ""
        finally:
            await page.close()


# -- the whole worker: Inbox, automatic mode, hand-over ------------------------------------------------------------


@pytest.mark.parametrize("path", ["signin_step.html", "signin_step_pages.html"])
async def test_automatic_two_step_login_runs_without_asking_and_leaks_nothing(env, path):  # noqa: F811
    account_id = await env.save(auto=True)
    task = await env.run(path)
    async with running_fleet():
        t = await wait_for(lambda: finished(task["id"]), timeout=40)
    b1 = (await subtasks(task["id"]))["b1"]
    assert t["status"] == "done" and b1["status"] == "done", b1["error"]
    events = [e["message"] for e in await env.p.fetch("select message from events where type = 'login'")]
    assert events == [f"Logged in as {USER} on 127.0.0.1 with the saved login (automatic) in two steps"]
    assert await env.p.fetchval("select last_used_at is not null from site_accounts where id = $1", account_id)
    await env.nothing_leaked(PASSWORD)


async def test_by_default_the_inbox_asks_first_for_a_two_step_login(env):  # noqa: F811
    await env.save(auto=False)
    task = await env.run("signin_step.html")
    async with running_fleet():
        card = await env.card()
        assert card["human_request"]["kind"] == "login_saved" and USER in card["human_request"]["message"]
        assert await env.p.fetchval("select last_used_at is null from site_accounts")  # nothing typed yet
        assert await hitl.respond(card["id"], "approve")
        t = await wait_for(lambda: finished(task["id"]), timeout=40)
    assert t["status"] == "done"
    await env.nothing_leaked(PASSWORD)


async def test_i_will_do_it_myself_types_nothing_on_a_username_page(env):  # noqa: F811
    await env.save(auto=False)
    task = await env.run("signin_step.html")
    async with running_fleet():
        card = await env.card()
        assert await hitl.respond(card["id"], "done")
        await wait_for(lambda: finished(task["id"]), timeout=40)
    assert await env.p.fetchval("select last_used_at is null from site_accounts")  # the password was never read


async def test_a_wrong_saved_password_is_tried_once_then_handed_to_the_operator(env):  # noqa: F811
    account_id = await env.save("not the right one", auto=True)
    task = await env.run("signin_step.html")
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


@pytest.mark.parametrize("path", ["newsletter.html", "contact.html", "signup_step.html"])
async def test_a_saved_login_is_never_offered_on_other_email_pages(env, path):  # noqa: F811
    await env.save(auto=True)
    task = await env.run(path)
    async with running_fleet():
        t = await wait_for(lambda: finished(task["id"]), timeout=40)
    assert t["status"] == "done"
    assert await env.p.fetchval("select count(*) from events where type = 'human.request'") == 0
    assert await env.p.fetchval("select last_used_at is null from site_accounts")  # never read


async def test_without_a_saved_login_a_username_page_is_left_alone(env):  # noqa: F811
    task = await env.run("signin_step.html")
    async with running_fleet():
        t = await wait_for(lambda: finished(task["id"]), timeout=40)
    assert t["status"] == "done"
    assert await env.p.fetchval("select count(*) from events where type in ('login', 'human.request')") == 0


async def test_without_a_saved_login_nothing_is_typed_even_when_asked_directly(browser_ws, site):
    """The full browser agent calls try_login between steps without checking for a saved login first."""
    ctx = RunContext(task={"id": None}, subtask={"id": None}, dependency_results={}, worker_id="t")
    async with CDP(browser_ws) as cdp:
        page = await Page.open(cdp, f"{site}/signin_step.html")
        try:
            assert await SavedLogin(ctx).try_login(_sender(page), f"{site}/signin_step.html") is None
            typed = await page.evaluate("[...document.querySelectorAll('input')].map(i => i.value).join('')")
            assert typed == ""
        finally:
            await page.close()
