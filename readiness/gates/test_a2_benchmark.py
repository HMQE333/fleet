"""A2: the hands can do the ordinary web, with the brain scripted. Each task is a real page in a real headless browser."""

import pytest

from fleet import llm
from fleet.fastbrowser import decide, engine
from fleet.fastbrowser.cdp import CDP
from readiness.gates.conftest import script

pytestmark = pytest.mark.no_db


class Hooks(engine.Hooks):
    async def on_page(self, state, page):
        return "stop" if state.get("login") else "ok"

    async def approve(self, action, state):
        return True


async def _run(ws, url, entries, monkeypatch, texts=None, **kw):
    monkeypatch.setattr(decide, "choose", script(list(entries)))
    queue = list(texts or [])

    async def field_text(goal, action, page, history, *, model, usage):
        return queue.pop(0) if queue else None

    monkeypatch.setattr(decide, "field_text", field_text)
    async with CDP(ws) as cdp:
        return await engine.run(cdp, url=url, goal="benchmark", hooks=Hooks(), usage=llm.Usage(), decide_model="m",
                                text_model="m", allow_private=True, **kw)


async def test_search_open_extract(browser_ws, site, monkeypatch):
    out = await _run(browser_ws, f"{site}/shop.html",
                     [("fill", "Search products"), ("click", "Search"), ("click", "Raspberry Pi 5 8GB - 399 zl"), "DONE"],
                     monkeypatch, texts=["raspberry pi 5"])
    assert out.status == "done", out.reason
    assert out.final["url"].endswith("product.html?id=1")


async def test_multi_field_form_with_select_and_checkbox(browser_ws, rsite, monkeypatch):
    out = await _run(browser_ws, f"{rsite}/form.html",
                     [("fill", "Full name"), ("fill", "Email"), ("select", "Country → Poland"), ("click", "Send me the newsletter"),
                      ("click", "Create profile"), "DONE"],
                     monkeypatch, texts=["Jan Kowalski", "jan@example.com"])
    assert out.status == "done", out.reason
    text = out.final["text"]
    assert "Jan Kowalski" in text and "jan@example.com" in text and "Poland" in text and "newsletter: yes" in text


async def test_pagination_to_the_item_on_page_three(browser_ws, site, monkeypatch):
    out = await _run(browser_ws, f"{site}/listing1.html",
                     [("click", "Next page"), ("click", "Next page"), "DONE"], monkeypatch)
    assert out.status == "done", out.reason
    assert "page 3" in out.final["title"].lower()


async def test_back_navigation(browser_ws, site, monkeypatch):
    out = await _run_with_back(browser_ws, site, monkeypatch)
    assert out.status == "done", out.reason
    assert "page 1" in out.final["title"].lower()


async def _run_with_back(ws, site, monkeypatch):
    calls = []

    async def choose(page, goal, history, *, model, usage, hint=None):
        calls.append(1)
        if len(calls) == 1:
            a = next(a for a in page["actions"] if a["kind"] == "click" and a["label"].startswith("Next page"))
            return {"choice": a["id"], "operation": "CLICK", "probabilities": {a["id"]: 0.9}, "goal_done": 0.0, "stuck": 0.0, "confidence": 0.9}
        if len(calls) == 2:
            return {"choice": "back", "operation": "BACK", "probabilities": {"back": 0.9}, "goal_done": 0.0, "stuck": 0.0, "confidence": 0.9}
        return {"choice": "DONE", "operation": "DONE", "probabilities": {}, "goal_done": 0.0, "stuck": 0.0, "confidence": 1.0}

    monkeypatch.setattr(decide, "choose", choose)
    async with CDP(ws) as cdp:
        return await engine.run(cdp, url=f"{site}/listing1.html", goal="b", hooks=Hooks(), usage=llm.Usage(), decide_model="m",
                                text_model="m", allow_private=True)


async def test_saved_login_is_typed_by_code(browser_ws, site):
    """Already covered end to end by tests/test_site_logins.py; here only the engine-level contract: a login page stops the hands."""
    monkey = pytest.MonkeyPatch()
    try:
        out = await _run(browser_ws, f"{site}/login.html", [], monkey)
    finally:
        monkey.undo()
    assert out.status == "stopped"


async def test_contenteditable_editor(browser_ws, rsite, monkeypatch):
    out = await _run(browser_ws, f"{rsite}/editor.html", [("fill", "Post body"), ("click", "Preview"), "DONE"], monkeypatch,
                     texts=["Hello from Fleet"])
    assert out.status == "done", out.reason
    assert "Preview: Hello from Fleet" in out.final["text"]
