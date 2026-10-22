"""Opt-in notifications to a webhook the operator entered.

The rules under test: nothing is sent unless set up, and then only for the kinds of events the operator ticked; the address must be an
https public address (checked again at send time), is stored encrypted and never returned; redirects are not followed; keys are scrubbed
from the text; at most 20 an hour; a failed delivery is logged without the address and never breaks anything.
"""

import asyncio
import http.server
import json
import threading

import httpx
import pytest

from fleet import db, events, notify, vault
from fleet.api import create_app
from fleet.fastbrowser import netguard

PANEL = {"X-Fleet-Client": "panel"}
TOKEN = "SECRETTOKEN123456"
REAL_GUARD = netguard.check_url_resolved


@pytest.fixture(autouse=True)
def fresh_state(monkeypatch):
    monkeypatch.setattr(notify, "_cache", None)
    monkeypatch.setattr(notify, "_sent", [])
    monkeypatch.setattr(notify, "_paused_until", 0.0)


@pytest.fixture
def hook(monkeypatch):
    """A local receiver standing in for the operator's webhook (plain http on a local port: the tests switch the address checks off)."""
    seen: list[dict] = []
    behaviour = {"status": 200, "location": None}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            seen.append({"path": self.path, "headers": dict(self.headers), "body": body.decode("utf-8")})
            self.send_response(302 if behaviour["location"] else behaviour["status"])
            if behaviour["location"]:
                self.send_header("Location", behaviour["location"])
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *a):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    async def public(url):
        return None

    monkeypatch.setattr(notify, "ALLOW_PLAIN_HTTP", True)
    monkeypatch.setattr(netguard, "check_url_resolved", public)
    yield type("Hook", (), {"url": f"http://127.0.0.1:{server.server_address[1]}/hooks/{TOKEN}", "seen": seen,
                            "behaviour": behaviour, "server": server})
    server.shutdown()


async def _task(prompt="Watch the RAM prices") -> object:
    p = await db.pool()
    return await p.fetchval("insert into tasks (prompt, status) values ($1, 'running') returning id", prompt)


async def _flush():
    if notify._tasks:
        await asyncio.gather(*list(notify._tasks))


# -- the address ----------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("url", [
    "http://hooks.example.com/x", "https://127.0.0.1/x", "https://localhost/x", "https://printer.local/x", "https://10.0.0.5/x",
    "https://user:pw@hooks.example.com/x", "https://hooks.example.com:8443/x", "ftp://hooks.example.com/x", "https://" + "a" * 2001,
])
async def test_only_a_public_https_address_can_be_saved(url, monkeypatch):
    monkeypatch.setattr(netguard, "check_url_resolved", REAL_GUARD)
    with pytest.raises(notify.NotifyError, match="can't be used"):
        await notify.save(url=url, on=["updates"], fmt="json")
    assert (await notify.config())["configured"] is False


async def test_a_public_https_address_is_accepted_and_unknown_choices_are_not(monkeypatch):
    async def public(url):
        return None

    monkeypatch.setattr(netguard, "check_url_resolved", public)
    saved = await notify.save(url="https://hooks.example.com/services/abc", on=["updates", "finished"], fmt="plain")
    assert saved["configured"] and saved["host"] == "hooks.example.com" and saved["on"] == ["finished", "updates"] and saved["format"] == "plain"
    with pytest.raises(notify.NotifyError, match="Unknown"):
        await notify.save(url=None, on=["everything"], fmt="json")
    with pytest.raises(notify.NotifyError, match="format"):
        await notify.save(url=None, on=[], fmt="xml")


async def test_the_address_is_encrypted_and_only_its_host_is_ever_shown(hook):
    await notify.save(url=hook.url, on=["updates"], fmt="json")
    shown = json.dumps(await notify.config())
    assert TOKEN not in shown and "127.0.0.1" in shown
    p = await db.pool()
    row = await p.fetchrow("select ciphertext from secrets where name = $1", notify.SECRET)
    assert TOKEN.encode() not in bytes(row["ciphertext"])
    assert TOKEN not in (await p.fetchval("select value::text from settings where key = 'notifications'"))
    await vault.get(notify.SECRET)
    assert vault.redact(f"oops {hook.url}") == "oops [NOTIFY_WEBHOOK_URL]"  # and if it ever reaches a log line, it is scrubbed


# -- sending ----------------------------------------------------------------------------------------------------------------


async def test_json_and_plain_messages_arrive_as_the_services_expect(hook):
    await notify.save(url=hook.url, on=["updates"], fmt="json")
    await notify.send("Price dropped")
    got = json.loads(hook.seen[0]["body"])
    assert hook.seen[0]["path"] == f"/hooks/{TOKEN}" and got["text"] == got["content"] == "Price dropped" and got["source"] == "fleet"
    await notify.save(url=None, on=["updates"], fmt="plain")
    await notify.send("Price dropped, again")
    assert hook.seen[1]["body"] == "Price dropped, again" and hook.seen[1]["headers"]["Title"] == "Fleet"


async def test_text_is_capped_and_keys_are_scrubbed_before_it_leaves(hook):
    await notify.save(url=hook.url, on=["updates"], fmt="json")
    vault.remember("sk-or-v1-0123456789abcdef", "OPENROUTER_API_KEY")
    await notify.send("key sk-or-v1-0123456789abcdef " + "x" * 1000)
    text = json.loads(hook.seen[0]["body"])["text"]
    assert "sk-or-v1" not in text and "[OPENROUTER_API_KEY]" in text and len(text) == notify.MAX_TEXT


async def test_redirects_are_never_followed(hook):
    await notify.save(url=hook.url, on=["updates"], fmt="json")
    hook.behaviour["location"] = "http://169.254.169.254/latest/meta-data/"
    with pytest.raises(notify.NotifyError, match="HTTP 302"):
        await notify.send("hello")
    assert len(hook.seen) == 1  # asked once, did not go where it was sent


async def test_the_address_is_checked_again_when_sending(hook, monkeypatch):
    await notify.save(url=hook.url, on=["updates"], fmt="json")

    async def rebinding(url):
        return "hooks.example.com points to a private address (10.0.0.9)"

    monkeypatch.setattr(netguard, "check_url_resolved", rebinding)  # the name now resolves inside the network
    with pytest.raises(notify.NotifyError, match="no longer allowed") as e:
        await notify.send("hello")
    assert hook.seen == [] and TOKEN not in str(e.value)


# -- which events become messages -----------------------------------------------------------------------------------------------


async def test_nothing_is_sent_unless_notifications_are_set_up_and_the_kind_is_ticked(hook):
    task = await _task()
    await events.emit("human.request", "Click “Send”", task_id=task)
    await _flush()
    assert hook.seen == []  # not set up at all
    await notify.save(url=hook.url, on=["updates"], fmt="json")
    await events.emit("human.request", "Click “Send”", task_id=task)
    await events.emit("task.status", "Done", task_id=task, data={"status": "done"})
    await _flush()
    assert hook.seen == []  # set up, but only "updates" is ticked
    await events.emit("scrape.changes", "1 new, 0 removed, 2 changed since the previous run", task_id=task)
    await _flush()
    assert len(hook.seen) == 1 and "Update [Watch the RAM prices]: 1 new, 0 removed, 2 changed" in json.loads(hook.seen[0]["body"])["text"]


async def test_each_kind_says_what_happened_and_chatter_never_does(hook):
    await notify.save(url=hook.url, on=list(notify.TRIGGERS), fmt="json")
    task = await _task()
    await events.emit("human.request", "Click “Send message” on https://shop.example/x", task_id=task)
    await events.emit("task.status", "Done", task_id=task, data={"status": "done"})
    await events.emit("task.status", "Planning failed: x", task_id=task, data={"status": "failed"})
    for chatter in (("step", "clicked"), ("log", "Page 1: 5 new items"), ("subtask.status", "x"), ("plan", "p"), ("task.status", "Reviewing")):
        await events.emit(chatter[0], chatter[1], task_id=task, data={"status": "running"})
    await _flush()
    texts = [json.loads(m["body"])["text"] for m in hook.seen]
    assert sorted(texts) == sorted([
        "Needs you [Watch the RAM prices]: Click “Send message” on https://shop.example/x",
        "Task done [Watch the RAM prices]", "Task failed [Watch the RAM prices]"])


async def test_unwatched_events_do_not_even_start_a_background_job(hook):
    await notify.save(url=hook.url, on=list(notify.TRIGGERS), fmt="json")
    await events.emit("step", "clicked")
    await events.emit("task.status", "Reviewing", data={"status": "reviewing"})
    assert not notify._tasks


async def test_at_most_twenty_an_hour_then_it_pauses_and_says_so_once(hook, monkeypatch):
    monkeypatch.setattr(notify, "MAX_PER_HOUR", 3)
    await notify.save(url=hook.url, on=["finished"], fmt="json")
    task = await _task()
    for _ in range(6):
        await events.emit("task.status", "Done", task_id=task, data={"status": "done"})
        await _flush()
    assert len(hook.seen) == 3
    p = await db.pool()
    assert await p.fetchval("select count(*) from events where type = 'notify.paused'") == 1


async def test_a_failed_delivery_is_logged_without_the_address_and_never_breaks_the_work(hook):
    await notify.save(url=hook.url, on=["finished"], fmt="json")
    task = await _task()
    hook.behaviour["status"] = 500
    await events.emit("task.status", "Done", task_id=task, data={"status": "done"})  # does not raise
    await _flush()
    hook.server.shutdown()
    hook.server.server_close()  # now nothing is listening at all
    await events.emit("task.status", "Done", task_id=task, data={"status": "done"})
    await _flush()
    p = await db.pool()
    failures = [r["message"] for r in await p.fetch("select message from events where type = 'notify.failed' order by id")]
    assert "answered HTTP 500" in failures[0] and "could not be delivered" in failures[1]
    assert not any(TOKEN in m or "127.0.0.1" in m for m in failures)


# -- the API ---------------------------------------------------------------------------------------------------------------------


@pytest.fixture
async def client():
    app = create_app(run_background=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8710") as c:
        yield c


async def test_the_api_sets_up_tests_keeps_and_removes_the_address_without_ever_returning_it(hook, client, monkeypatch):
    body = {"url": hook.url, "on": ["updates", "needs_you"], "format": "json"}
    assert (await client.put("/api/settings/notifications", json=body)).status_code == 403  # CSRF guard
    r = await client.put("/api/settings/notifications", json=body, headers=PANEL)
    assert r.status_code == 200 and r.json()["configured"] and TOKEN not in r.text
    assert TOKEN not in (await client.get("/api/settings/notifications")).text
    assert (await client.post("/api/settings/notifications/test")).status_code == 403
    assert (await client.post("/api/settings/notifications/test", headers=PANEL)).status_code == 200
    assert "Fleet test" in json.loads(hook.seen[0]["body"])["text"]
    keep = await client.put("/api/settings/notifications", json={"on": ["finished"], "format": "plain"}, headers=PANEL)  # no url: keeps it
    assert keep.json()["configured"] and keep.json()["on"] == ["finished"] and keep.json()["format"] == "plain"
    monkeypatch.setattr(notify, "ALLOW_PLAIN_HTTP", False)  # the real rule again: https only
    bad = await client.put("/api/settings/notifications", json={"url": "http://x.example/y", "on": []}, headers=PANEL)
    assert bad.status_code == 400 and "https" in bad.json()["detail"]
    assert (await client.get("/api/settings/notifications")).json()["host"] == "127.0.0.1"  # the refused one saved nothing
    assert (await client.put("/api/settings/notifications", json={"on": ["nope"]}, headers=PANEL)).status_code == 400
    gone = await client.put("/api/settings/notifications", json={"url": "", "on": []}, headers=PANEL)
    assert gone.json()["configured"] is False and gone.json()["host"] is None
    assert (await client.post("/api/settings/notifications/test", headers=PANEL)).status_code == 400  # nothing to send to


async def test_an_error_that_mentions_the_address_never_shows_it(hook, monkeypatch):
    await notify.save(url=hook.url, on=["finished"], fmt="json")

    async def broken(self, url, **kw):
        raise httpx.ConnectError(f"cannot connect to {url}")  # some libraries put the whole address in their messages

    monkeypatch.setattr(httpx.AsyncClient, "post", broken)
    with pytest.raises(notify.NotifyError) as e:
        await notify.send("hello")
    assert TOKEN not in str(e.value) and "127.0.0.1" not in str(e.value) and "ConnectError" in str(e.value)
