"""The fast browser engine against a real headless browser and local pages, with Jev scripted.

Covers what can't be faked: the page reader, hit-tested clicks, typing, freshness, screenshots —
plus the safety gates and recorded-path replay. Skipped when no Edge/Chrome is installed.
"""

import pytest

from fleet import llm
from fleet.fastbrowser import decide, engine
from fleet.fastbrowser.cdp import CDP

from .conftest import scripted

pytestmark = pytest.mark.no_db

class Recorder(engine.Hooks):
    def __init__(self, approve: bool = False):
        self.approvals, self.steps, self.pages, self._approve = [], [], [], approve

    async def on_page(self, state, page):
        self.pages.append(state)
        return "stop" if state.get("login") else "ok"

    async def approve(self, action, state):
        self.approvals.append(action["label"])
        return self._approve

    async def on_step(self, record, state):
        self.steps.append((record, bool(state.get("screenshot"))))


async def _run(browser_ws, url, hooks, **kw):
    async with CDP(browser_ws) as cdp:
        return await engine.run(cdp, url=url, goal="test goal", hooks=hooks, usage=llm.Usage(),
                                decide_model="typesafe/jev-1.13", text_model="fast", allow_private=True, **kw)


@pytest.fixture
def fake_text(monkeypatch):
    async def field_text(goal, action, page, history, *, model, usage):
        return "raspberry pi 5"

    monkeypatch.setattr(decide, "field_text", field_text)


async def test_searches_types_clicks_and_finishes(browser_ws, site, monkeypatch, fake_text):
    calls = []
    monkeypatch.setattr(decide, "choose", scripted([("fill", "Search products"), ("click", "Search"), "DONE"], calls))
    hooks = Recorder()
    out = await _run(browser_ws, f"{site}/shop.html", hooks)
    assert out.status == "done", out.reason
    assert "Results for raspberry pi 5" in out.final["text"]
    assert [t["kind"] for t in out.trace] == ["fill", "click"]
    assert out.trace[0]["value"] == "raspberry pi 5"
    assert len(hooks.steps) == 2 and all(has_shot for _, has_shot in hooks.steps)
    assert not hooks.approvals  # nothing irreversible
    labels = [a["label"] for a in hooks.pages[0]["actions"]]
    assert "Skip to content" in labels  # the reader sees it…
    assert all(a["label"] != "Skip to content" for a in decide.filter_actions(hooks.pages[0]["actions"]))  # …Jev doesn't


async def test_recorded_path_replays_without_model_calls(browser_ws, site, monkeypatch, fake_text):
    guide = [{"kind": "fill", "label": "Search products", "value": "raspberry pi 5"},
             {"kind": "click", "label": "Search", "value": None}]
    calls = []
    monkeypatch.setattr(decide, "choose", scripted(["DONE"], calls))  # only asked once, at the end
    out = await _run(browser_ws, f"{site}/shop.html", Recorder(), guide=guide)
    assert out.status == "done", out.reason
    assert len(calls) == 1 and calls[0] is None  # no hint left: both recorded steps ran without Jev
    assert all(s["guided"] for s in out.steps)


async def test_irreversible_click_needs_the_operator(browser_ws, site, monkeypatch, fake_text):
    script = [("fill", "Message"), ("click", "Send message")]
    monkeypatch.setattr(decide, "choose", scripted(list(script), []))
    hooks = Recorder(approve=False)
    out = await _run(browser_ws, f"{site}/compose.html", hooks)
    assert out.status == "rejected" and hooks.approvals == ["Send message"]
    assert "Message sent!" not in out.final["text"] and not out.acted

    monkeypatch.setattr(decide, "choose", scripted([*script, "DONE"], []))
    out = await _run(browser_ws, f"{site}/compose.html", Recorder(approve=True))
    assert out.status == "done" and out.acted
    assert "Message sent!" in out.final["text"]


async def test_login_page_is_handed_to_the_operator(browser_ws, site, monkeypatch):
    monkeypatch.setattr(decide, "choose", scripted([], []))
    hooks = Recorder()
    out = await _run(browser_ws, f"{site}/login.html", hooks)
    assert out.status == "stopped"
    assert hooks.pages[0]["login"] is True
    assert all(a.get("role") != "textbox" or a["label"] != "Password" for a in hooks.pages[0]["actions"])


@pytest.fixture(scope="module")
def other_site():
    """The same fixture pages on a second port: another origin, so a frame served from here is cross-origin."""
    import functools
    import http.server
    import threading

    from .conftest import FIXTURES

    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(FIXTURES))
    handler.log_message = lambda *a: None
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


async def test_same_origin_frames_are_read_and_clicked_cross_origin_ones_left_alone(browser_ws, site, other_site, monkeypatch):
    """A3: the button in the page's own frame is offered with the frame's text and can be clicked (the hit test maps the
    frame's geometry to the window); the frame from another origin is not entered: its button isn't offered, its text
    isn't read."""
    monkeypatch.setattr(decide, "choose", scripted([("click", "Reveal secret"), "DONE"], []))
    hooks = Recorder()
    out = await _run(browser_ws, f"{site}/widget.html?other={other_site}", hooks)
    assert out.status == "done", out.reason
    assert "Secret: 4711" in out.final["text"] and "Secret: 4711" in out.final["full_text"]
    first = hooks.pages[0]
    assert [a["label"] for a in first["actions"]].count("Reveal secret") == 1  # ours once, the foreign one never
    assert first["text"].count("Widget content.") == 1
    assert out.steps[0]["page_changed"]


async def test_a_file_input_takes_only_the_operators_path(browser_ws, site, monkeypatch, tmp_path):
    """A4: a file input is offered as `upload` (never as a click that would open the native dialog); the path comes from
    `hooks.pick_file` alone, so the default hook blocks the step, and a hook that answers sets the file through the protocol."""
    doc = tmp_path / "report.txt"
    doc.write_text("hello", encoding="utf-8")
    monkeypatch.setattr(decide, "choose", scripted([("upload", "Attachment"), ("click", "Attach"), "DONE"], []))
    hooks = Recorder()
    out = await _run(browser_ws, f"{site}/attach.html", hooks)
    assert out.status == "blocked" and "No file was chosen" in out.reason and not out.trace
    offered = [(a["kind"], a["label"]) for a in hooks.pages[0]["actions"] if a.get("role") == "upload"]
    assert offered == [("upload", "Attachment")]

    class Chooser(Recorder):
        async def pick_file(self, action, state):
            return str(doc)

    monkeypatch.setattr(decide, "choose", scripted([("upload", "Attachment"), ("click", "Attach"), "DONE"], []))
    out = await _run(browser_ws, f"{site}/attach.html", Chooser())
    assert out.status == "done", out.reason
    assert "Attached: report.txt (5 bytes)" in out.final["text"]
    assert [(t["kind"], t["value"]) for t in out.trace] == [("upload", str(doc)), ("click", None)]


async def _page_targets(browser_ws) -> list[str]:
    async with CDP(browser_ws) as cdp:
        return [t["url"] for t in (await cdp.send("Target.getTargets"))["targetInfos"] if t["type"] == "page"]


async def test_a_click_that_opens_a_new_tab_is_followed_and_every_tab_is_closed_at_the_end(browser_ws, site, monkeypatch):
    """A6: the new tab becomes the page the engine observes; when the step ends both tabs are gone."""
    monkeypatch.setattr(decide, "choose", scripted([("click", "Open the report in a new tab"), "DONE"], []))
    hooks = Recorder()
    out = await _run(browser_ws, f"{site}/reports.html", hooks)
    assert out.status == "done", out.reason
    assert "Opened in new tab: 9321" in out.final["text"] and out.final["url"].endswith("/report.html")
    assert [p["url"].rsplit("/", 1)[1] for p in hooks.pages] == ["reports.html", "report.html"]
    assert out.steps[0]["page_changed"]
    assert not [u for u in await _page_targets(browser_ws) if "report" in u]


async def test_back_from_a_new_tab_closes_it_and_returns_to_the_tab_that_opened_it(browser_ws, site, monkeypatch):
    queue = [("click", "Open the report in a new tab"), "back", "DONE"]

    async def choose(page, goal, history, *, model, usage, hint=None):
        entry = queue.pop(0)
        if entry == "back":
            return {"choice": "back", "operation": "BACK", "probabilities": {"back": 0.9}, "goal_done": 0.0, "stuck": 0.0,
                    "confidence": 0.9}
        return await scripted([entry], [])(page, goal, history, model=model, usage=usage, hint=hint)

    monkeypatch.setattr(decide, "choose", choose)
    hooks = Recorder()
    out = await _run(browser_ws, f"{site}/reports.html", hooks)
    assert out.status == "done", out.reason
    assert out.final["url"].endswith("/reports.html") and "Opened in new tab" not in out.final["text"]
    assert [t["kind"] for t in out.trace] == ["click", "back"]
    assert [p["url"].rsplit("/", 1)[1] for p in hooks.pages] == ["reports.html", "report.html", "reports.html"]


async def test_a_wrong_turn_the_engine_took_is_walked_back_out_of(browser_ws, site, monkeypatch):
    """Jev is asked about the current page only, so it can never answer "the page before this one still had the
    way on". When it reports BLOCKED where there is nothing left to read, and the engine is the one that clicked
    into that branch, the engine steps back and offers the page it came from again (code decides, not the model)."""
    script = [("click", "Raspberry Pi 5 8GB - 399 zl"), "BLOCKED", ("click", "Raspberry Pi 5 4GB - 299 zl"), "DONE"]
    monkeypatch.setattr(decide, "choose", scripted(script, []))
    hooks = Recorder()
    out = await _run(browser_ws, f"{site}/results.html?q=raspberry+pi+5", hooks)
    assert out.status == "done", out.reason
    assert out.final["url"].endswith("/product.html?id=2")
    assert [t["kind"] for t in out.trace] == ["click", "back", "click"]
    assert [s["operation"] for s in out.steps] == ["CLICK", "BACK_OUT", "CLICK"]
    assert [p["url"].rsplit("/", 1)[1] for p in hooks.pages] == [
        "results.html?q=raspberry+pi+5", "product.html?id=1", "results.html?q=raspberry+pi+5", "product.html?id=2"]


async def test_the_start_page_is_handed_over_rather_than_backed_out_of(browser_ws, site, monkeypatch):
    """The back-out only undoes the engine's own navigation: BLOCKED where it has not moved yet is still blocked,
    and it never steps back out of the run it was given."""
    monkeypatch.setattr(decide, "choose", scripted(["BLOCKED"], []))
    out = await _run(browser_ws, f"{site}/results.html?q=raspberry+pi+5", Recorder())
    assert out.status == "blocked"
    assert out.reason == "Jev found no supported action that makes progress"


async def test_a_new_tab_outside_the_allowed_domains_is_closed_and_the_engine_returns(browser_ws, site, monkeypatch):
    """The new tab's address goes through the same checks as any page: a host outside the allowlist is left at once."""
    other = site.replace("127.0.0.1", "localhost")
    monkeypatch.setattr(decide, "choose", scripted([("click", "Open the report elsewhere"), "DONE"], []))
    hooks = Recorder()
    out = await _run(browser_ws, f"{site}/reports.html?other={other}", hooks, allowed_domains=["127.0.0.1"])
    assert out.status == "done", out.reason
    assert out.final["url"].startswith(site) and "Opened in new tab" not in out.final["text"]
    assert "localhost" not in " ".join(await _page_targets(browser_ws))


async def test_private_addresses_are_refused(browser_ws, site):
    async with CDP(browser_ws) as cdp:
        out = await engine.run(cdp, url=f"{site}/shop.html", goal="x", hooks=Recorder(), usage=llm.Usage(),
                               decide_model="m", text_model="m")
    assert out.status == "blocked" and "private" in out.reason


@pytest.fixture
def slow_site():
    """A site whose second page sends its head at once and its body 1.6 s later, the way a real page loads over a slow link."""
    import http.server
    import threading
    import time

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            if self.path.startswith("/slow"):
                self.wfile.write(b"<!doctype html><html><head><title>Slow article</title></head>")
                self.wfile.flush()
                time.sleep(1.6)
                self.wfile.write(b"<body><h1>Slow article</h1><p>Population: 486000.</p></body></html>")
            else:
                self.wfile.write(b'<!doctype html><title>Start</title><body><a href="/slow">Open the article</a></body>')

        def log_message(self, *a):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


async def test_waits_for_a_page_that_is_still_loading_instead_of_giving_up(browser_ws, slow_site, monkeypatch):
    """Real run: Wikipedia's article was mid-load, observe() gave up after half a second ("Document is navigating") and the cheap
    engine handed the task over. A page that takes seconds to get its body must be waited for."""
    monkeypatch.setattr(decide, "choose", scripted([("click", "Open the article"), "DONE"], []))
    out = await _run(browser_ws, f"{slow_site}/", Recorder())
    assert out.status == "done", out.reason
    assert "Population: 486000" in out.final["text"]
