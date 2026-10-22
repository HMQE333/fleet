"""A voice session (gate V4): parsed commands act on a live page, with the engine's gates in code."""

import pytest

from fleet import voice
from fleet.fastbrowser.cdp import CDP

pytestmark = pytest.mark.no_db


@pytest.fixture
async def cdp(browser_ws):
    async with CDP(browser_ws) as c:
        yield c


async def run(session, text):
    cmd = voice.parse(text)
    assert cmd is not None, text
    return await session.execute(cmd)


async def test_a_commitment_click_waits_for_approve_and_reject_drops_it(cdp, site):
    session = await voice.Session.open(cdp, f"{site}/compose.html", allow_private=True)
    try:
        r = await run(session, "click Send message")
        assert r["ok"] and r["needs_approval"] and "approval" in r["said"]
        assert "Message sent!" not in await session.page_text()
        r = await run(session, "reject")
        assert r["ok"] and "Send message" in r["said"] and session.pending is None
        assert "Message sent!" not in await session.page_text()
        r = await run(session, "approve")
        assert r["said"] == "Nothing waits for approval."
        await run(session, "kliknij Send message")
        r = await run(session, "zgoda")
        assert r["ok"] and r["said"] == "Clicked Send message"
        assert "Message sent!" in await session.page_text()
    finally:
        await session.close()


async def test_nothing_is_typed_into_a_password_field(cdp, site):
    session = await voice.Session.open(cdp, f"{site}/login.html", allow_private=True)
    try:
        await session.page.evaluate("document.querySelector('input[type=password]').focus()")
        r = await run(session, "type hunter2")  # the grammar lets a bare word through; the session checks the field
        assert not r["ok"] and "password" in r["said"] and "hunter2" not in r["said"]
        assert await session.page.evaluate("document.querySelector('input[type=password]').value") == ""
        await session.page.evaluate("document.activeElement.blur()")
        r = await run(session, "type hello there")  # no focus: the first listed field (never a password field)
        assert r["ok"] and r["said"] == "Typed into Email."
        assert await session.page.evaluate("document.querySelector('input[type=email]').value") == "hello there"
        assert await session.page.evaluate("document.querySelector('input[type=password]').value") == ""
    finally:
        await session.close()


async def test_search_uses_the_search_box_on_the_page_and_enter_submits(cdp, site):
    session = await voice.Session.open(cdp, f"{site}/shop.html", allow_private=True)
    try:
        r = await run(session, "szukaj ddr4")
        assert r["ok"] and r["said"] == "Searching for ddr4"
        assert (await session.page.url()).endswith("results.html?q=ddr4")
    finally:
        await session.close()


async def test_labels_match_by_prefix_and_nth_picks_a_link(cdp, site):
    session = await voice.Session.open(cdp, f"{site}/listing1.html", allow_private=True)
    try:
        r = await run(session, "click nothing like this")
        assert not r["ok"] and "can't see" in r["said"]
        r = await run(session, "click corsair")
        assert r["ok"] and r["said"] == "Clicked Corsair Vengeance 16GB DDR4 3600"
        assert "corsair-vengeance" in await session.page.url()
        r = await run(session, "wróć")
        assert r["ok"] and "RAM kits" in await session.page_text()
        r = await run(session, "read the page")
        assert r["ok"] and "Kingston Fury" in r["text"] and r["said"].startswith("Home")
        r = await run(session, "otwórz drugi wynik")  # the page's links in order, noise ("Log in") dropped: Home, Kingston
        assert r["ok"] and "kingston" in await session.page.url()
    finally:
        await session.close()


async def test_a_private_address_is_refused_unless_allowed(cdp, site):
    with pytest.raises(voice.VoiceError):
        await voice.Session.open(cdp, f"{site}/listing1.html")
    session = await voice.Session.open(cdp, f"{site}/listing1.html", allow_private=True)
    try:
        r = await run(session, "new task compare prices")
        assert not r["ok"] and "planner" in r["said"]
        r = await run(session, "type my password hunter2")
        assert not r["ok"] and "hunter2" not in r["said"]
        r = await run(session, "next tab")
        assert r["ok"] and r["said"].startswith("Tab ")
        r = await run(session, "help")
        assert r["ok"] and r["said"] == voice.HELP
        assert [h["kind"] for h in session.history] == ["task", "refused", "tab", "help"]
    finally:
        await session.close()
