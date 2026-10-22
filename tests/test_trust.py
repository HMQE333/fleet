"""Trust rules: "approve and always allow this click" for one exact label on one exact site and profile.

The rules under test (RULES.md §4): rules only come from an Inbox card the operator answered; the match is exact
(label, host, profile); payments, deleting, accepting terms, signing up and generic labels can never be trusted;
the operator can delete a rule at any time; every automatic approval is logged and still marks the step as acted.
"""

import httpx
import pytest

from fleet import adspower, db, hitl, llm, openrouter, site_accounts, trust, worker
from fleet.api import create_app
from fleet.config import settings
from fleet.executors import browser_fast
from fleet.fastbrowser import decide, netguard, policy

from .conftest import finished, new_task, running_fleet, scripted, sub, subtasks, wait_for

PANEL = {"X-Fleet-Client": "panel"}


# -- what can never be trusted -----------------------------------------------------------------------------------


@pytest.mark.no_db
@pytest.mark.parametrize("label", ["Send", "Send message", "Post now", "Reply", "Share", "Upload", "Follow", "Wyślij",
                                   "Udostępnij", "Facebook"])
def test_routine_commitments_can_be_trusted(label):
    assert policy.can_trust(label)


@pytest.mark.no_db
@pytest.mark.parametrize("label", [
    "Place order", "Pay now", "Buy now", "Checkout", "Subscribe", "Donate", "Transfer", "Kup teraz", "Zapłać",  # money
    "Delete", "Remove item", "Usuń", "Cancel order", "Unsubscribe", "Save changes",  # can't be undone
    "Accept all", "I agree", "Sign up", "Register", "Zarejestruj",  # commits the operator
    "OK", "Confirm", "Yes", "Submit", "Continue", "Potwierdź", "",  # meaning depends on the page
])
def test_money_deleting_terms_sign_ups_and_generic_labels_are_never_trusted(label):
    assert not policy.can_trust(label)


@pytest.mark.no_db
def test_the_card_offers_nothing_for_local_or_untrusted_clicks():
    assert trust.offer("http://127.0.0.1:8000/x", "Send message") is None  # logins/rules are for public sites
    assert trust.offer("https://mail.example.com/compose", "Place order") is None
    assert trust.offer("https://www.example.com/compose", "Send  message") == {"domain": "example.com", "label": "Send message"}


# -- the store -----------------------------------------------------------------------------------------------------


async def _profile(name: str) -> str:
    p = await db.pool()
    return await p.fetchval(
        "insert into profiles (adspower_id, name, enabled) values ($1, $2, true) returning id", f"id-{name}", name)


async def test_a_rule_covers_exactly_one_label_on_one_host_for_one_profile():
    mine, other = await _profile("Mine"), await _profile("Other")
    rule = await trust.add(mine, "example.com", "Send message")
    assert (await trust.add(mine, "example.com", "  send   MESSAGE "))["id"] == rule["id"]  # the same rule, once
    assert (await trust.find("https://www.example.com/compose?x=1", "SEND message", mine))["id"] == rule["id"]
    assert await trust.find("https://mail.example.com/", "Send message", mine) is None  # a subdomain is another site
    assert await trust.find("https://evil-example.com/", "Send message", mine) is None
    assert await trust.find("https://example.com/", "Send", mine) is None  # another label
    assert await trust.find("https://example.com/", "Send message now", mine) is None  # no partial matches
    assert await trust.find("https://example.com/", "Send message", other) is None  # another profile
    assert await trust.find("https://example.com/", "Send message", None) is None
    listed = await trust.list_rules()
    assert [(r["domain"], r["label"], r["profile_name"]) for r in listed] == [("example.com", "Send message", "Mine")]
    assert await trust.delete(rule["id"]) and not await trust.delete(rule["id"])
    assert await trust.find("https://example.com/", "Send message", mine) is None


async def test_untrustable_clicks_cannot_be_stored_and_a_planted_rule_is_ignored():
    mine = await _profile("Mine")
    for label in ("Place order", "Delete", "Confirm", "Accept all"):
        with pytest.raises(trust.TrustError):
            await trust.add(mine, "example.com", label)
    p = await db.pool()  # even a row written behind the API's back doesn't switch approval off
    await p.execute("insert into trust_rules (profile_id, domain, label, key) values ($1, 'example.com', 'Place order', 'place order')", mine)
    assert await trust.find("https://example.com/cart", "Place order", mine) is None


@pytest.fixture
async def client():
    app = create_app(run_background=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8710") as c:
        yield c


async def test_api_lists_and_revokes_rules_but_cannot_create_them(client):
    mine = await _profile("Mine")
    rule = await trust.add(mine, "example.com", "Send message")
    listed = (await client.get("/api/trust-rules")).json()
    assert [(r["domain"], r["label"], r["profile_name"], r["uses"]) for r in listed] == [("example.com", "Send message", "Mine", 0)]
    r = await client.post("/api/trust-rules", json={"domain": "example.com", "label": "Post"}, headers=PANEL)
    assert r.status_code == 405  # rules come only from an approval card
    assert (await client.delete(f"/api/trust-rules/{rule['id']}")).status_code == 403  # CSRF guard
    assert (await client.delete("/api/trust-rules/00000000-0000-0000-0000-000000000000", headers=PANEL)).status_code == 404
    assert (await client.delete(f"/api/trust-rules/{rule['id']}", headers=PANEL)).status_code == 200
    assert (await client.get("/api/trust-rules")).json() == []
    p = await db.pool()
    assert await p.fetchval("select count(*) from events where type = 'trust.deleted'") == 1
    assert (await client.post("/api/subtasks/00000000-0000-0000-0000-000000000000/respond",
                              json={"decision": "approve", "remember": True}, headers=PANEL)).status_code == 409


async def test_only_an_approval_can_carry_the_remember_flag():
    p = await db.pool()
    task = await new_task()
    st = {}
    for decision in ("approve", "reject", "done"):
        st[decision] = await p.fetchval(
            "insert into subtasks (task_id, round, key, title, kind, instructions, status, human_request) "
            "values ($1, 1, $2, 't', 'browser_fast', 'x', 'needs_human', $3) returning id",
            task["id"], decision, {"kind": "approval", "message": "m", "url": None, "requested_at": "2026-01-01T00:00:00+00:00"})
        assert await hitl.respond(st[decision], decision, remember=True)
    answers = {d: await p.fetchval("select human_response from subtasks where id = $1", i) for d, i in st.items()}
    assert [answers[d]["remember"] for d in ("approve", "reject", "done")] == [True, False, False]


# -- the whole worker: Inbox card, remembering, using the rule ----------------------------------------------------------


@pytest.fixture
async def env(fake, browser_ws, site, monkeypatch, tmp_path):
    """The real browser_fast worker over a headless Edge; Jev clicks the one button the test names."""
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
    monkeypatch.setattr(llm, "complete", answer)
    monkeypatch.setattr(site_accounts, "normalize_domain", lambda v: "127.0.0.1")  # the test site is local

    async def run(page: str, label: str):
        monkeypatch.setattr(decide, "choose", scripted([("click", label), "DONE"], []))
        fake.plan.subtasks = [sub("b1", kind="browser_fast", url=f"{site}/{page}")]
        return await new_task()

    async def card():
        return await wait_for(lambda: p.fetchrow("select * from subtasks where status = 'needs_human'"), timeout=30)

    async def rules():
        return await p.fetch("select * from trust_rules")

    return type("Env", (), {"p": p, "run": staticmethod(run), "card": staticmethod(card),
                            "rules": staticmethod(rules), "profile_id": profile_id})


async def test_approve_and_always_allow_saves_a_rule_and_the_next_run_does_not_ask(env):
    task = await env.run("compose.html", "Send message")
    async with running_fleet():
        card = await env.card()
        assert card["human_request"]["kind"] == "approval"
        assert card["human_request"]["offer"] == {"domain": "127.0.0.1", "label": "Send message"}
        assert await env.rules() == []  # nothing is remembered until the operator says so
        assert await hitl.respond(card["id"], "approve", remember=True)
        await wait_for(lambda: finished(task["id"]), timeout=40)
    (rule,) = await env.rules()
    assert (rule["domain"], rule["label"], rule["profile_id"], rule["uses"]) == ("127.0.0.1", "Send message", env.profile_id, 0)
    saved = await env.p.fetchval("select message from events where type = 'trust' order by id limit 1")
    assert "Rule saved" in saved and "Settings → Trust rules" in saved

    again = await env.run("compose.html", "Send message")  # a second run, e.g. a scheduled one
    async with running_fleet():
        t = await wait_for(lambda: finished(again["id"]), timeout=40)
    b1 = (await subtasks(again["id"]))["b1"]
    assert t["status"] == "done" and b1["status"] == "done", b1["error"]
    asked = await env.p.fetchval("select count(*) from events where type = 'human.request' and task_id = $1", again["id"])
    assert asked == 0
    used = await env.p.fetchval("select message from events where type = 'trust' and task_id = $1", again["id"])
    assert "Approved by your trust rule" in used and "Send message" in used
    assert b1["side_effects"] is True  # an approved action still means the step must not be retried automatically
    (rule,) = await env.rules()
    assert rule["uses"] == 1 and rule["last_used_at"] is not None


async def test_a_plain_approval_remembers_nothing_and_asks_again(env):
    for _ in range(2):
        task = await env.run("compose.html", "Send message")
        async with running_fleet():
            card = await env.card()
            assert await hitl.respond(card["id"], "approve")  # no "always allow"
            await wait_for(lambda task=task: finished(task["id"]), timeout=40)
        assert await env.rules() == []


async def test_a_rejected_card_never_creates_a_rule_even_if_asked_to(env):
    task = await env.run("compose.html", "Send message")
    async with running_fleet():
        card = await env.card()
        assert await hitl.respond(card["id"], "reject", remember=True)
        await wait_for(lambda: finished(task["id"]), timeout=40)
    assert await env.rules() == []


async def test_payments_are_never_offered_and_never_remembered(env):
    task = await env.run("checkout.html", "Place order")
    async with running_fleet():
        card = await env.card()
        assert "offer" not in card["human_request"] and "payment" in card["human_request"]["message"]
        assert await hitl.respond(card["id"], "approve", remember=True)  # a hostile client asks anyway
        await wait_for(lambda: finished(task["id"]), timeout=40)
    b1 = (await subtasks(task["id"]))["b1"]
    assert b1["status"] == "done", b1["error"]  # this once was approved; it just isn't remembered
    assert await env.rules() == []
    # and a rule planted for it is ignored: the next run still asks
    await env.p.execute("insert into trust_rules (profile_id, domain, label, key) values ($1, '127.0.0.1', 'Place order', 'place order')", env.profile_id)
    again = await env.run("checkout.html", "Place order")
    async with running_fleet():
        card = await env.card()
        assert await hitl.respond(card["id"], "reject")
        await wait_for(lambda: finished(again["id"]), timeout=40)


async def test_a_rule_for_another_profile_does_not_apply(env):
    other = await _profile("Other")
    await trust.add(other, "127.0.0.1", "Send message")
    task = await env.run("compose.html", "Send message")
    async with running_fleet():
        card = await env.card()  # still asks: the rule belongs to a different profile
        assert await hitl.respond(card["id"], "reject")
        await wait_for(lambda: finished(task["id"]), timeout=40)
