"""A3: the hands see into same-origin frames (an embedded editor, a widget) and act there."""

import pytest

from fleet import llm
from fleet.fastbrowser import decide, engine
from fleet.fastbrowser.cdp import CDP
from readiness.gates.conftest import script

pytestmark = pytest.mark.no_db


async def test_a_button_inside_an_iframe_is_offered_and_clicked(browser_ws, rsite, monkeypatch):
    monkeypatch.setattr(decide, "choose", script([("click", "Reveal secret"), "DONE"], []))
    async with CDP(browser_ws) as cdp:
        out = await engine.run(cdp, url=f"{rsite}/frame.html", goal="reveal", hooks=engine.Hooks(), usage=llm.Usage(),
                               decide_model="m", text_model="m", allow_private=True)
    assert out.status == "done", out.reason
    assert "Secret: 4711" in out.final.get("full_text", "") or "Secret: 4711" in out.final["text"]
