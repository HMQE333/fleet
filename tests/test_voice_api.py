"""The voice endpoints (gate V2): a task from words, a session on a local profile, commands against it, refusals.

The browser and the CDP connection are stand-ins here (tests/test_voice_session.py drives a real one); what these tests
pin down is the API's shape, its guards, and that a refused or misunderstood utterance never reaches a page.
"""

import pytest
from httpx import ASGITransport, AsyncClient

from fleet import api as fleet_api
from fleet import browsers, voice
from fleet.api import create_app

HEADERS = {"X-Fleet-Client": "panel", "Origin": "http://127.0.0.1:8710", "Host": "127.0.0.1:8710"}


class FakeSession:
    def __init__(self):
        self.ran: list[voice.Command] = []
        self.closed = False

    async def execute(self, cmd):
        self.ran.append(cmd)
        return {"ok": True, "said": voice.describe(cmd), "took": 0.01}

    async def close(self):
        self.closed = True


class FakeLive:
    opened: list[tuple[str, str]] = []
    last: "FakeLive | None" = None

    def __init__(self):
        self.session = FakeSession()
        self.closed = False

    @classmethod
    async def connect(cls, ws_url, url, *, allow_private=False, profile_id=None):
        cls.opened.append((ws_url, url))
        cls.last = cls()
        return cls.last

    async def close(self):
        self.closed = True
        await self.session.close()


@pytest.fixture
async def client(monkeypatch):
    FakeLive.opened, FakeLive.last = [], None
    monkeypatch.setattr(voice, "Live", FakeLive)
    opened_profiles = []

    async def fake_open(profile, *, visible=False):
        opened_profiles.append((profile["name"], visible))
        return browsers.Opened(cdp_url="ws://127.0.0.1:1/devtools/browser/fake", kind=browsers.LOCAL, debug_port="1")

    monkeypatch.setattr(browsers, "open", fake_open)
    monkeypatch.setattr(fleet_api, "voice_sessions", {})
    app = create_app(run_background=False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8710") as c:
        c.opened_profiles = opened_profiles
        yield c


async def test_new_task_words_become_the_task_and_a_plain_sentence_is_the_task_itself(client):
    r = await client.post("/api/voice/task", json={"text": "nowe zadanie porównaj ceny RAM"}, headers=HEADERS)
    assert r.status_code == 201, r.text
    task = (await client.get(f"/api/tasks/{r.json()['task_id']}", headers=HEADERS)).json()["task"]
    assert task["prompt"] == "porównaj ceny RAM" and task["require_approval"] and task["plan_review"] == "auto"
    r = await client.post("/api/voice/task", json={"text": "  check the weather in Gdańsk  "}, headers=HEADERS)
    assert r.status_code == 201 and r.json()["said"] == "New task: check the weather in Gdańsk"
    assert (await client.post("/api/voice/task", json={"text": "x"}, headers=HEADERS)).status_code == 400
    r = await client.post("/api/voice/task", json={"text": "new task pay with 4111 1111 1111 1111"}, headers=HEADERS)
    assert r.status_code == 400 and "4111" not in r.text


async def test_a_session_opens_a_visible_tab_on_the_voice_profile_and_commands_run_there(client):
    r = await client.post("/api/voice/session", json={"url": "https://example.com"}, headers=HEADERS)
    assert r.status_code == 201, r.text
    sid = r.json()["session_id"]
    assert client.opened_profiles == [("Voice", True)] and FakeLive.opened == [("ws://127.0.0.1:1/devtools/browser/fake", "https://example.com")]
    prof = (await client.get("/api/profiles", headers=HEADERS)).json()
    mine = next(p for p in prof if p["id"] == r.json()["profile_id"])
    assert mine["kind"] == "local" and mine["name"] == "Voice" and not mine["general_use"] and not mine["headless"]

    r = await client.post("/api/voice/command", json={"session_id": sid, "text": "przewiń w dół"}, headers=HEADERS)
    assert r.status_code == 200, r.text
    assert r.json() == {"command": {"kind": "scroll", "direction": "down"},
                        "result": {"ok": True, "said": "Scrolling down", "took": 0.01}}
    assert [c.kind for c in FakeLive.last.session.ran] == ["scroll"]

    # a second session reuses the same profile (one "Voice" profile, however many tabs)
    r = await client.post("/api/voice/session", json={"url": "https://example.org"}, headers=HEADERS)
    assert r.status_code == 201 and r.json()["profile_id"] == mine["id"]
    assert len([p for p in (await client.get("/api/profiles", headers=HEADERS)).json() if p["name"] == "Voice"]) == 1

    r = await client.delete(f"/api/voice/session/{sid}", headers=HEADERS)
    assert r.status_code == 200 and FakeLive.last.closed is False  # the last one is the second session
    assert (await client.post("/api/voice/command", json={"session_id": sid, "text": "scroll down"}, headers=HEADERS)).status_code == 404
    assert (await client.delete(f"/api/voice/session/{sid}", headers=HEADERS)).status_code == 404


async def test_refused_and_misunderstood_words_never_reach_the_page(client):
    r = await client.post("/api/voice/session", json={"url": "https://example.com"}, headers=HEADERS)
    sid = r.json()["session_id"]
    for text in ("type my password hunter2", "wpisz hasło kotek123", "type card 4111 1111 1111 1111"):
        r = await client.post("/api/voice/command", json={"session_id": sid, "text": text}, headers=HEADERS)
        assert r.status_code == 400, text
        assert "hunter2" not in r.text and "kotek" not in r.text and "4111" not in r.text
    r = await client.post("/api/voice/command", json={"session_id": sid, "text": "blah blah nothing"}, headers=HEADERS)
    assert r.status_code == 400 and r.json()["detail"] == "I didn't understand that."
    assert FakeLive.last.session.ran == []
    # a command without a session: refused words are still 400 (before any lookup), the rest is 404
    assert (await client.post("/api/voice/command", json={"text": "type pin 1234"}, headers=HEADERS)).status_code == 400
    assert (await client.post("/api/voice/command", json={"text": "scroll down"}, headers=HEADERS)).status_code == 404


async def test_a_spoken_task_in_the_command_route_needs_no_session(client):
    r = await client.post("/api/voice/command", json={"text": "new task compare RAM prices in five shops"}, headers=HEADERS)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["command"] == {"kind": "task", "prompt": "compare RAM prices in five shops"} and body["result"]["ok"]
    task = (await client.get(f"/api/tasks/{body['result']['task_id']}", headers=HEADERS)).json()["task"]
    assert task["prompt"] == "compare RAM prices in five shops"


async def test_a_private_or_unreachable_page_is_an_error_not_a_session(client, monkeypatch):
    async def refuse(ws_url, url, **_):
        raise voice.VoiceError("Refused: private address")

    monkeypatch.setattr(FakeLive, "connect", refuse)
    r = await client.post("/api/voice/session", json={"url": "http://127.0.0.1:8710/"}, headers=HEADERS)
    assert r.status_code == 400 and "private" in r.text

    async def no_browser(profile, *, visible=False):
        raise browsers.BrowserError("No Chrome or Edge found")

    monkeypatch.setattr(browsers, "open", no_browser)
    assert (await client.post("/api/voice/session", json={"url": "https://example.com"}, headers=HEADERS)).status_code == 502


async def test_the_voice_routes_are_guarded_like_every_other_route(client):
    for path in ("/api/voice/task", "/api/voice/session", "/api/voice/command"):
        assert (await client.post(path, json={})).status_code == 403, path
        assert (await client.post(path, json={}, headers={**HEADERS, "Origin": "https://evil.example"})).status_code == 403, path
