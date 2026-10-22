"""V4: a parsed navigation command acts within a second (voice must beat the mouse).

Expected shape: `fleet.voice.Session` (or equivalent) wraps a fast-engine Page over CDP and has
`await session.execute(command) -> dict` with `{"ok": bool, "said": str}`; `said` is what the panel reads aloud.
"""

import importlib
import time

import pytest

from fleet.fastbrowser.cdp import CDP

pytestmark = pytest.mark.no_db


async def test_scroll_back_and_click_execute_within_a_second(browser_ws, site):
    try:
        voice = importlib.import_module("fleet.voice")
    except ImportError as e:
        pytest.fail(f"fleet.voice is missing: {e}")
    async with CDP(browser_ws) as cdp:
        session = await voice.Session.open(cdp, f"{site}/listing1.html", allow_private=True)
        try:
            for text in ("scroll down", "click Next page", "go back"):
                cmd = voice.parse(text)
                assert cmd is not None, text
                started = time.perf_counter()
                result = await session.execute(cmd)
                took = time.perf_counter() - started
                assert result["ok"], f"{text}: {result}"
                assert took <= 1.0, f"{text} took {took:.2f}s"
            assert "RAM kits" in (await session.page_text())
        finally:
            await session.close()
