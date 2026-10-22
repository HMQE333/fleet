"""Macros (fleet/macro.py): validated step lists the hands run with no model call, under the usual gates."""

import pytest

from fleet import llm, macro
from fleet.fastbrowser import engine
from fleet.fastbrowser.cdp import CDP

pytestmark = pytest.mark.no_db


def test_validation_names_the_broken_step():
    with pytest.raises(macro.MacroError, match="steps.0.kind"):
        macro.validate({"steps": [{"kind": "teleport"}]})
    with pytest.raises(macro.MacroError, match="fill needs value"):
        macro.validate({"steps": [{"kind": "fill", "label": "Search"}]})
    with pytest.raises(macro.MacroError, match="http"):
        macro.validate({"steps": [{"kind": "navigate", "url": "file:///etc/passwd"}]})
    with pytest.raises(macro.MacroError):
        macro.validate({"steps": []})
    m = macro.validate({"name": "search", "steps": [{"kind": "navigate", "url": "https://example.com"}, {"kind": "scroll"}]})
    assert m.name == "search" and m.steps[1].direction == "down"


@pytest.mark.parametrize("label", ["Password", "Hasło", "Card number", "CVV", "One-time code", "Kod SMS", "PIN"])
def test_a_macro_never_types_a_secret(label):
    with pytest.raises(macro.MacroError, match="RULES.md"):
        macro.validate({"steps": [{"kind": "fill", "label": label, "value": "x"}]})


async def test_runs_the_shop_search_with_zero_model_calls_and_records_the_path(browser_ws, site):
    m = {"steps": [
        {"kind": "navigate", "url": f"{site}/shop.html"},
        {"kind": "fill", "label": "Search products", "value": "raspberry pi 5"},
        {"kind": "click", "label": "Search"},
        {"kind": "wait_for", "text": "Results for", "seconds": 5},
        {"kind": "extract", "name": "title", "what": "title"},
        {"kind": "extract", "name": "page"},
        {"kind": "assert_text", "text": "Raspberry Pi 5 8GB"},
    ]}
    usage = llm.Usage()
    async with CDP(browser_ws) as cdp:
        out = await macro.run(cdp, m, usage=usage, allow_private=True)
    assert out.status == "done", out.reason
    assert usage.cost_usd == 0 and usage.input_tokens == 0
    assert out.extracted["title"] == "Results" and "Raspberry Pi 5 4GB" in out.extracted["page"]
    assert out.trace == [{"kind": "fill", "label": "Search products", "value": "raspberry pi 5"},
                         {"kind": "click", "label": "Search", "value": None}]
    assert [s["kind"] for s in out.steps] == ["navigate", "fill", "click", "wait_for", "extract", "extract", "assert_text"]


async def test_a_commitment_click_waits_for_the_operator(browser_ws, site):
    class Hooks(engine.Hooks):
        asked: list[str] = []

        async def approve(self, action, state):
            self.asked.append(action["label"])
            return False

    m = {"steps": [{"kind": "navigate", "url": f"{site}/compose.html"}, {"kind": "fill", "label": "Message", "value": "hi"},
                   {"kind": "click", "label": "Send message"}]}
    async with CDP(browser_ws) as cdp:
        out = await macro.run(cdp, m, hooks=Hooks(), usage=llm.Usage(), allow_private=True)
    assert out.status == "rejected" and Hooks.asked == ["Send message"]
    assert "Message sent!" not in (out.final or {}).get("full_text", "") and not out.acted

    class Yes(engine.Hooks):
        async def approve(self, action, state):
            return True

    async with CDP(browser_ws) as cdp:
        out = await macro.run(cdp, m, hooks=Yes(), usage=llm.Usage(), allow_private=True)
    assert out.status == "done" and out.acted and "Message sent!" in out.final["full_text"]


async def test_a_missing_element_and_an_unmet_wait_stop_honestly(browser_ws, site):
    async with CDP(browser_ws) as cdp:
        out = await macro.run(cdp, {"steps": [{"kind": "navigate", "url": f"{site}/shop.html"}, {"kind": "click", "label": "Checkout now"}]},
                              usage=llm.Usage(), allow_private=True)
        assert out.status == "blocked" and "Checkout now" in out.reason and "offered" in out.reason
        out = await macro.run(cdp, {"steps": [{"kind": "navigate", "url": f"{site}/shop.html"}, {"kind": "wait_for", "text": "never", "seconds": 1}]},
                              usage=llm.Usage(), allow_private=True)
        assert out.status == "stuck" and "never" in out.reason
        out = await macro.run(cdp, {"steps": [{"kind": "click", "label": "Search"}]}, usage=llm.Usage(), allow_private=True)
        assert out.status == "error" and "navigate" in out.reason


async def test_private_addresses_are_refused_like_everywhere_else(browser_ws, site):
    async with CDP(browser_ws) as cdp:
        out = await macro.run(cdp, {"steps": [{"kind": "navigate", "url": f"{site}/shop.html"}]}, usage=llm.Usage())
    assert out.status == "blocked" and "private" in out.reason


async def test_select_back_and_scroll(browser_ws, site):
    m = {"steps": [
        {"kind": "navigate", "url": f"{site}/listing1.html"},
        {"kind": "click", "label": "Next page"},
        {"kind": "assert_text", "text": "Lexar Hades"},
        {"kind": "back"},
        {"kind": "assert_text", "text": "Patriot Viper"},
        {"kind": "scroll", "direction": "down"},
    ]}
    async with CDP(browser_ws) as cdp:
        out = await macro.run(cdp, m, usage=llm.Usage(), allow_private=True)
    assert out.status == "done", out.reason
    assert out.trace[0]["label"].startswith("Next page") and out.trace[1]["kind"] == "back"
