"""B2: the brain writes a macro once; code runs it, no model in the loop.

Expected shape: `fleet.macro` with
- `Macro` (pydantic): steps of kind navigate(url) | click(label) | fill(label, value) | select(label, value) |
  wait_for(text, seconds) | extract(name, what) | back | scroll(direction) | assert_text(text)
- `validate(data) -> Macro` raising `MacroError` on bad input
- `await run(cdp, macro, *, hooks, usage, allow_private=False) -> Outcome` using the fast engine's page and gates
  (approvals through hooks, never a password field), with `usage.cost_usd == 0` when nothing needed a model.
"""

import importlib

import pytest

from fleet import llm
from fleet.fastbrowser.cdp import CDP

pytestmark = pytest.mark.no_db


def _macro():
    try:
        return importlib.import_module("fleet.macro")
    except ImportError as e:
        pytest.fail(f"fleet.macro is missing: {e}")


def test_bad_macros_are_refused():
    macro = _macro()
    with pytest.raises(macro.MacroError):
        macro.validate({"steps": [{"kind": "teleport"}]})
    with pytest.raises(macro.MacroError):
        macro.validate({"steps": [{"kind": "fill", "label": "Password", "value": "x"}]})  # never a password
    m = macro.validate({"steps": [{"kind": "navigate", "url": "https://example.com"}, {"kind": "click", "label": "More"}]})
    assert len(m.steps) == 2


async def test_a_macro_runs_the_shop_search_with_zero_model_calls(browser_ws, site):
    macro = _macro()
    m = macro.validate({"steps": [
        {"kind": "navigate", "url": f"{site}/shop.html"},
        {"kind": "fill", "label": "Search products", "value": "raspberry pi 5"},
        {"kind": "click", "label": "Search"},
        {"kind": "wait_for", "text": "Results for", "seconds": 5},
        {"kind": "extract", "name": "first", "what": "first result"},
        {"kind": "assert_text", "text": "Raspberry Pi 5 8GB"},
    ]})
    usage = llm.Usage()
    async with CDP(browser_ws) as cdp:
        out = await macro.run(cdp, m, usage=usage, allow_private=True)
    assert out.status in ("done", "goal_achieved"), out.reason
    assert usage.cost_usd == 0 and usage.input_tokens == 0
    assert "Raspberry Pi 5 8GB" in (out.final or {}).get("text", "")
    assert [t["kind"] for t in out.trace] == ["fill", "click"]  # the replayable path the hands took


async def test_a_macro_asks_before_an_irreversible_click(browser_ws, site):
    from fleet.fastbrowser import engine

    macro = _macro()

    class Hooks(engine.Hooks):
        asked = []

        async def approve(self, action, state):
            self.asked.append(action["label"])
            return False

    m = macro.validate({"steps": [
        {"kind": "navigate", "url": f"{site}/compose.html"},
        {"kind": "fill", "label": "Message", "value": "hi"},
        {"kind": "click", "label": "Send message"},
    ]})
    async with CDP(browser_ws) as cdp:
        out = await macro.run(cdp, m, hooks=Hooks(), usage=llm.Usage(), allow_private=True)
    assert out.status == "rejected" and Hooks.asked == ["Send message"]
