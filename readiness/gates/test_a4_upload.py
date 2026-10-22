"""A4: a file input can be set, from a path the operator approved (never a path the page or the model chose).

Expected shape: the page reader offers file inputs as kind "upload"; `Hooks.pick_file(action, state) -> str | None`
returns a local path (Fleet's hook asks the operator in the Inbox); the engine sets it with DOM.setFileInputFiles."""

import pytest

from fleet import llm
from fleet.fastbrowser import decide, engine
from fleet.fastbrowser.cdp import CDP
from readiness.gates.conftest import script

pytestmark = pytest.mark.no_db


async def test_upload_from_an_approved_path(browser_ws, rsite, monkeypatch, tmp_path):
    doc = tmp_path / "report.txt"
    doc.write_text("hello", encoding="utf-8")

    class Hooks(engine.Hooks):
        async def pick_file(self, action, state):
            return str(doc)

    monkeypatch.setattr(decide, "choose", script([("upload", "Attachment"), ("click", "Attach"), "DONE"], []))
    async with CDP(browser_ws) as cdp:
        out = await engine.run(cdp, url=f"{rsite}/upload.html", goal="attach the report", hooks=Hooks(), usage=llm.Usage(),
                               decide_model="m", text_model="m", allow_private=True)
    assert out.status == "done", out.reason
    assert "Attached: report.txt" in out.final["text"]
