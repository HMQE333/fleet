"""A1: Fleet drives a local Chrome profile of its own (no AdsPower).

Expected shape (the loop may choose another as long as the asserts hold):
- `fleet.browsers` opens a browser for a profile row: `await browsers.open(profile)` → object with `.cdp_url`,
  `await browsers.close(profile)`; AdsPower profiles keep working through the same calls.
- a profile row has `kind` ('adspower' | 'local') and local profiles keep their user data under data/profiles/<id>.
- `fleet.profiles.add_local(name, headless=True)` inserts an enabled, general-use local profile.
"""

import importlib

import pytest

pytestmark = pytest.mark.no_db


def _module(name: str):
    try:
        return importlib.import_module(name)
    except ImportError as e:
        pytest.fail(f"{name} is missing: {e}")


def test_a_local_profile_can_be_added_and_opened_and_closed():
    browsers = _module("fleet.browsers")
    profiles = _module("fleet.profiles")
    assert hasattr(profiles, "add_local"), "fleet.profiles.add_local(name, headless=True) is missing"
    assert hasattr(browsers, "open") and hasattr(browsers, "close"), "fleet.browsers.open/close are missing"


async def test_local_browser_opens_headless_and_answers_cdp(tmp_path):
    browsers = _module("fleet.browsers")
    profile = {"id": "00000000-0000-0000-0000-00000000a1a1", "kind": "local", "adspower_id": "local:test", "name": "Local test",
               "headless": True, "data_dir": str(tmp_path / "profile")}
    opened = await browsers.open(profile)
    try:
        assert opened.cdp_url.startswith("ws://")
        from fleet.fastbrowser.cdp import CDP

        async with CDP(opened.cdp_url) as cdp:
            version = await cdp.send("Browser.getVersion")
            assert "Chrome" in version.get("product", "") or "Edg" in version.get("product", "")
    finally:
        await browsers.close(profile)


async def test_both_browser_workers_use_the_browsers_module_not_adspower_directly():
    import inspect

    from fleet.executors import browser, browser_fast

    for mod in (browser, browser_fast):
        src = inspect.getsource(mod)
        assert "adspower.client()" not in src, f"{mod.__name__} still opens profiles through AdsPower directly"
        assert "browsers.open(" in src, f"{mod.__name__} does not use fleet.browsers.open"
