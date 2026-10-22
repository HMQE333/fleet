"""Answering waiting Inbox cards together.

The rules under test (ROADMAP S4, RULES.md §4): the operator answers only the cards they were shown (explicit ids);
stopping may always be done in bulk, but approving only for what a click approves anyway (a saved login, an approval
that is explicitly not a payment); "I've handled it", sign-ups, captchas, take-overs and payments always need their
own answer; every answer is logged like a single one.
"""

import httpx
import pytest

from fleet import db, hitl
from fleet.api import create_app
from fleet.fastbrowser import decide

from .conftest import finished, new_task, running_fleet, scripted, sub, wait_for
from .test_trust import env  # noqa: F401  (the real browser_fast worker over a headless Edge)

PANEL = {"X-Fleet-Client": "panel"}
UNKNOWN = "00000000-0000-0000-0000-000000000000"


@pytest.mark.no_db
@pytest.mark.parametrize("request_,decision,allowed", [
    ({"kind": "approval", "payment": False}, "approve", True),
    ({"kind": "approval", "payment": True}, "approve", False),  # a payment always needs its own answer
    ({"kind": "approval"}, "approve", False),  # no explicit "not a payment": not eligible
    ({"kind": "login_saved"}, "approve", True),
    ({"kind": "register"}, "approve", False), ({"kind": "challenge"}, "approve", False),
    ({"kind": "login"}, "approve", False), ({"kind": "takeover"}, "approve", False), ({"kind": "help"}, "approve", False),
    ({"kind": "verify_email"}, "approve", False),
    ({"kind": "approval", "payment": True}, "reject", True), ({"kind": "register"}, "reject", True),
    ({"kind": "challenge"}, "reject", True), ({"kind": "takeover"}, "reject", True),
    ({"kind": "approval", "payment": False}, "done", False), ({"kind": "login"}, "done", False),  # never "I handled it"
])
def test_what_may_be_answered_together(request_, decision, allowed):
    assert hitl.bulk_allows(request_, decision) is allowed


@pytest.fixture
async def client():
    app = create_app(run_background=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8710") as c:
        yield c


async def _waiting(**cards: dict) -> dict:
    """One task with a waiting card per name; returns {name: subtask id}."""
    p = await db.pool()
    task = await p.fetchval("insert into tasks (prompt, status) values ('x', 'running') returning id")
    ids = {}
    for name, request in cards.items():
        ids[name] = await p.fetchval(
            "insert into subtasks (task_id, round, key, title, kind, instructions, status, human_request) "
            "values ($1, 1, $2, $2, 'browser_fast', 'x', 'needs_human', $3) returning id", task, name,
            {"message": name, "url": None, "requested_at": "2026-01-01T00:00:00+00:00", **request})
    return ids


async def _answer(subtask_id) -> dict | None:
    p = await db.pool()
    return await p.fetchval("select human_response from subtasks where id = $1", subtask_id)


async def test_approving_together_only_answers_what_a_click_would_approve_anyway(client):
    ids = await _waiting(
        send1={"kind": "approval", "payment": False}, send2={"kind": "approval", "payment": False},
        pay={"kind": "approval", "payment": True}, old={"kind": "approval"}, login={"kind": "login_saved"},
        signup={"kind": "register"}, captcha={"kind": "challenge"}, take={"kind": "takeover"})
    r = await client.post("/api/inbox/respond-many", headers=PANEL, json={"ids": [str(i) for i in ids.values()], "decision": "approve"})
    assert r.status_code == 200
    body = r.json()
    assert set(body["answered"]) == {str(ids[n]) for n in ("send1", "send2", "login")}
    assert {s["id"]: s["reason"] for s in body["skipped"]} == {
        str(ids[n]): "needs your own answer" for n in ("pay", "old", "signup", "captcha", "take")}
    for name in ("send1", "send2", "login"):
        assert await _answer(ids[name]) == {"decision": "approve", "note": "", "remember": False}
    for name in ("pay", "old", "signup", "captcha", "take"):
        assert await _answer(ids[name]) is None  # still waiting for the operator
    p = await db.pool()
    assert await p.fetchval("select count(*) from events where type = 'human.response'") == 3  # each logged like a single answer


async def test_stopping_together_works_for_every_kind_and_carries_the_note(client):
    ids = await _waiting(pay={"kind": "approval", "payment": True}, signup={"kind": "register"},
                         captcha={"kind": "challenge"}, take={"kind": "takeover"}, help={"kind": "help"})
    r = await client.post("/api/inbox/respond-many", headers=PANEL, json={
        "ids": [str(i) for i in ids.values()], "decision": "reject", "note": "wrong account"})
    assert len(r.json()["answered"]) == 5 and r.json()["skipped"] == []
    assert await _answer(ids["pay"]) == {"decision": "reject", "note": "wrong account", "remember": False}


async def test_only_the_cards_the_operator_was_shown_are_answered(client):
    ids = await _waiting(a={"kind": "approval", "payment": False}, b={"kind": "approval", "payment": False},
                         new={"kind": "approval", "payment": False})  # "new" arrived after the operator looked
    shown = [str(ids["a"]), str(ids["b"])]
    r = await client.post("/api/inbox/respond-many", headers=PANEL, json={"ids": shown, "decision": "approve"})
    assert len(r.json()["answered"]) == 2 and await _answer(ids["new"]) is None


async def test_cards_that_are_gone_or_already_answered_are_reported_not_answered_twice(client):
    ids = await _waiting(a={"kind": "approval", "payment": False}, b={"kind": "approval", "payment": False})
    assert await hitl.respond(ids["a"], "reject")  # answered on its own a moment ago
    r = await client.post("/api/inbox/respond-many", headers=PANEL, json={
        "ids": [str(ids["a"]), str(ids["b"]), str(ids["b"]), UNKNOWN], "decision": "approve"})
    body = r.json()
    assert body["answered"] == [str(ids["b"])]  # a duplicate id counts once
    assert {s["id"] for s in body["skipped"]} == {str(ids["a"]), UNKNOWN}
    assert all(s["reason"] == "not waiting any more" for s in body["skipped"])
    assert (await _answer(ids["a"]))["decision"] == "reject"  # the earlier answer stands


async def test_the_endpoint_checks_its_input(client):
    ids = await _waiting(a={"kind": "approval", "payment": False})
    ok = {"ids": [str(ids["a"])], "decision": "approve"}
    assert (await client.post("/api/inbox/respond-many", json=ok)).status_code == 403  # CSRF guard
    assert (await client.post("/api/inbox/respond-many", headers=PANEL, json={**ok, "ids": []})).status_code == 422
    assert (await client.post("/api/inbox/respond-many", headers=PANEL, json={**ok, "ids": [UNKNOWN] * 51})).status_code == 422
    assert (await client.post("/api/inbox/respond-many", headers=PANEL, json={**ok, "decision": "done"})).status_code == 422
    assert (await client.post("/api/inbox/respond-many", headers=PANEL, json={**ok, "ids": ["not-an-id"]})).status_code == 422
    assert await _answer(ids["a"]) is None  # nothing above answered anything


# -- the real worker marks payments, and two waiting steps are approved together ----------------------------------------


async def test_the_fast_browser_says_whether_an_approval_is_a_payment(env):  # noqa: F811
    task = await env.run("compose.html", "Send message")
    async with running_fleet():
        card = await env.card()
        assert card["human_request"]["kind"] == "approval" and card["human_request"]["payment"] is False
        assert await hitl.respond(card["id"], "approve")
        await wait_for(lambda: finished(task["id"]), timeout=40)
    task = await env.run("checkout.html", "Place order")
    async with running_fleet():
        card = await env.card()
        assert card["human_request"]["payment"] is True
        r = await hitl.respond_many([card["id"]], "approve")
        assert r["answered"] == [] and r["skipped"][0]["reason"] == "needs your own answer"  # a payment: only you
        assert await hitl.respond(card["id"], "reject")
        await wait_for(lambda: finished(task["id"]), timeout=40)


async def test_two_steps_waiting_for_the_same_kind_of_approval_are_approved_in_one_go(env, client, monkeypatch, fake, site):  # noqa: F811
    await env.p.execute("insert into profiles (adspower_id, name, enabled, general_use) values ('k2', 'Second', true, true)")
    monkeypatch.setattr(decide, "choose", scripted([("click", "Send message"), ("click", "Send message"), "DONE", "DONE"], []))
    fake.plan.subtasks = [sub("b1", kind="browser_fast", url=f"{site}/compose.html"), sub("b2", kind="browser_fast", url=f"{site}/compose.html")]
    task = await new_task()
    async with running_fleet():
        await wait_for(lambda: env.p.fetchval("select count(*) = 2 from subtasks where status = 'needs_human'"), timeout=40)
        ids = [str(r["id"]) for r in await env.p.fetch("select id from subtasks where status = 'needs_human'")]
        r = await client.post("/api/inbox/respond-many", headers=PANEL, json={"ids": ids, "decision": "approve"})
        assert len(r.json()["answered"]) == 2
        t = await wait_for(lambda: finished(task["id"]), timeout=40)
    assert t["status"] == "done"
    assert await env.p.fetchval("select count(*) from events where type = 'human.response'") == 2
    assert await env.p.fetchval("select count(*) from trust_rules") == 0  # approving together never creates a rule
