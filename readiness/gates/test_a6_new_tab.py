"""A6: a link that opens a new tab doesn't lose the hands: the new tab becomes the page, and the result is read there."""

import pytest

from fleet import llm
from fleet.fastbrowser import decide, engine
from fleet.fastbrowser.cdp import CDP
from readiness.gates.conftest import script

pytestmark = pytest.mark.no_db


async def test_the_new_tab_is_followed(browser_ws, rsite, monkeypatch):
    monkeypatch.setattr(decide, "choose", script([("click", "Open the report in a new tab"), "DONE"], []))
    async with CDP(browser_ws) as cdp:
        out = await engine.run(cdp, url=f"{rsite}/popup.html", goal="open the report", hooks=engine.Hooks(), usage=llm.Usage(),
                               decide_model="m", text_model="m", allow_private=True)
    assert out.status == "done", out.reason
    assert "Opened in new tab: 9321" in out.final["text"]
