"""Local browser profiles: Fleet starts its own Chrome / Edge, no AdsPower (fleet/browsers.py, READINESS.md gate A1)."""

import pathlib

import pytest

from fleet import adspower, browsers, db, profiles
from fleet.fastbrowser.cdp import CDP

HAS_BROWSER = browsers.executable() is not None


@pytest.mark.no_db
@pytest.mark.skipif(not HAS_BROWSER, reason="no Chrome/Edge installed")
async def test_a_local_profile_opens_headless_answers_cdp_and_closes(tmp_path):
    profile = {"id": "00000000-0000-0000-0000-00000000a1a1", "kind": "local", "name": "Local test", "headless": True,
               "data_dir": str(tmp_path / "profile")}
    opened = await browsers.open(profile)
    try:
        assert opened.kind == "local" and opened.cdp_url.startswith("ws://127.0.0.1:")
        again = await browsers.open(profile)
        assert again.cdp_url == opened.cdp_url  # the same running browser, not a second one
        async with CDP(opened.cdp_url) as cdp:
            product = (await cdp.send("Browser.getVersion")).get("product", "")
        assert "Chrome" in product or "Edg" in product, product
        assert (tmp_path / "profile").is_dir()
    finally:
        await browsers.close(profile)
    assert browsers._alive(profile["id"]) is None


@pytest.mark.no_db
async def test_an_adspower_profile_still_goes_through_the_local_api(monkeypatch):
    calls = []

    async def start(adspower_id, headless=False):
        calls.append(("start", adspower_id))
        return adspower.OpenBrowser(adspower_id, "ws://ads/devtools/browser/1", "9222")

    async def stop(adspower_id):
        calls.append(("stop", adspower_id))

    monkeypatch.setattr(adspower.client(), "start", start)
    monkeypatch.setattr(adspower.client(), "stop", stop)
    profile = {"id": "p1", "adspower_id": "k1", "name": "Ads"}  # no kind: rows from before migration 019 default to AdsPower
    opened = await browsers.open(profile)
    assert opened.kind == "adspower" and opened.cdp_url == "ws://ads/devtools/browser/1"
    await browsers.close(profile)
    assert calls == [("start", "k1"), ("stop", "k1")]


@pytest.mark.no_db
async def test_a_missing_browser_is_a_readable_error(monkeypatch, tmp_path):
    monkeypatch.setattr(browsers, "executable", lambda: None)
    with pytest.raises(browsers.BrowserError, match="FLEET_BROWSER_EXE"):
        await browsers.open({"id": "x", "kind": "local", "name": "L", "data_dir": str(tmp_path)})


@pytest.mark.no_db
def test_where_to_look_tells_the_operator_about_headless_profiles():
    assert "AdsPower" in browsers.window_name({"name": "A", "kind": "adspower"})
    note = browsers.window_name({"name": "L", "kind": "local", "headless": True})
    assert "AdsPower" not in note and "headless" in note and "Profiles page" in note
    assert "headless" not in browsers.window_name({"name": "L", "kind": "local", "headless": False})


async def test_add_local_creates_an_enabled_general_use_profile():
    r = await profiles.add_local("  Scratch  ", headless=False, notes="for tests")
    assert r["name"] == "Scratch" and r["kind"] == "local" and r["enabled"] and r["general_use"] and not r["headless"]
    assert r["adspower_id"].startswith("local:")
    p = await db.pool()
    assert await p.fetchval("select count(*) from profiles where kind = 'local'") == 1
    with pytest.raises(ValueError):
        await profiles.add_local("   ")


async def test_the_executors_and_the_api_open_profiles_through_browsers_only():
    src = pathlib.Path(__file__).resolve().parents[1] / "src" / "fleet"
    for name in ("executors/browser.py", "executors/browser_fast.py"):
        text = (src / name).read_text(encoding="utf-8")
        assert "adspower.client()" not in text and "browsers.open(" in text, name
    assert "browsers.open(" in (src / "api.py").read_text(encoding="utf-8")
