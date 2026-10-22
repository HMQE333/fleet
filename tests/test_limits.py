"""Limits editable in the panel: what can be set, how far, and that the workers really use the saved values.

The rules under test: only whitelisted caps, each within a hard range, everything validated before anything is written, null
removes an override, a bad stored value is ignored, and an executor reads the saved limit (no restart).
"""

import httpx
import pytest

from fleet import db, limits, scrape_store
from fleet.api import create_app
from fleet.config import settings
from fleet.executors import RunContext
from fleet.registration import Registration

from .test_crawl import _crawl, site_dir  # noqa: F401  (a small crawlable site)
from .test_scrape import _scrape_task, local_site_is_fine, shop  # noqa: F401  (the three-page shop)

PANEL = {"X-Fleet-Client": "panel"}


@pytest.fixture
async def client():
    app = create_app(run_background=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8710") as c:
        yield c


def _row(rows: list[dict], name: str) -> dict:
    return next(r for r in rows if r["name"] == name)


async def test_without_overrides_the_limits_are_the_defaults_and_follow_the_settings(monkeypatch):
    rows = await limits.snapshot()
    assert {r["name"] for r in rows} == set(limits.SPECS) and not any(r["overridden"] for r in rows)
    assert _row(rows, "crawl_max_pages")["value"] == settings().crawl_max_pages
    monkeypatch.setattr(settings(), "crawl_max_pages", 7)  # e.g. a changed .env
    assert (await limits.effective()).crawl_max_pages == 7
    assert (await limits.effective()).crawl_text_chars == settings().crawl_text_chars  # anything else reads through


async def test_a_saved_limit_wins_and_null_puts_the_default_back():
    rows = await limits.put({"crawl_max_pages": 12, "scrape_max_items": 40})
    assert _row(rows, "crawl_max_pages")["value"] == 12 and _row(rows, "crawl_max_pages")["overridden"] is True
    assert (await limits.effective()).scrape_max_items == 40
    rows = await limits.put({"crawl_max_pages": None})  # the other override stays
    assert _row(rows, "crawl_max_pages")["overridden"] is False and _row(rows, "scrape_max_items")["value"] == 40
    assert (await limits.effective()).crawl_max_pages == settings().crawl_max_pages


@pytest.mark.parametrize("values", [
    {"crawl_max_pages": 0}, {"crawl_max_pages": 501}, {"crawl_max_depth": -1}, {"crawl_max_depth": 7},
    {"scrape_max_details": 201}, {"registrations_per_day": 11}, {"crawl_max_seconds": 5},
    {"fetch_min_delay_seconds": 0},  # politeness is not editable
    {"data_dir": 1}, {"nonsense": 3}, {"crawl_max_pages": True},
])
async def test_out_of_range_unknown_and_politeness_limits_are_refused_and_nothing_is_written(values):
    await limits.put({"scrape_max_items": 40})
    with pytest.raises(limits.LimitError):
        await limits.put({"crawl_max_depth": 2, **values})  # one bad value spoils the whole save
    rows = await limits.snapshot()
    assert _row(rows, "crawl_max_depth")["overridden"] is False and _row(rows, "scrape_max_items")["value"] == 40


async def test_a_bad_value_found_in_the_database_is_ignored_not_trusted():
    p = await db.pool()
    await p.execute("insert into settings (key, value) values ('limits', $1)", {"crawl_max_pages": 999999, "scrape_max_items": "many", "crawl_max_depth": 2, "bogus": 1})
    eff = await limits.effective()
    assert eff.crawl_max_pages == settings().crawl_max_pages and eff.scrape_max_items == settings().scrape_max_items
    assert eff.crawl_max_depth == 2  # the one good value is kept


async def test_the_api_lists_saves_validates_and_is_protected(client):
    listed = (await client.get("/api/settings/limits")).json()
    assert {r["name"] for r in listed} == set(limits.SPECS)
    body = {"values": {"crawl_max_pages": 25}}
    assert (await client.put("/api/settings/limits", json=body)).status_code == 403  # CSRF guard
    r = await client.put("/api/settings/limits", json=body, headers=PANEL)
    assert r.status_code == 200 and _row(r.json(), "crawl_max_pages")["value"] == 25
    bad = await client.put("/api/settings/limits", json={"values": {"crawl_max_pages": 9999}}, headers=PANEL)
    assert bad.status_code == 400 and "from 1 to 500" in bad.json()["detail"]
    assert (await client.put("/api/settings/limits", json={"values": {"crawl_max_pages": 2.5}}, headers=PANEL)).status_code == 422
    assert (await client.put("/api/settings/limits", json={"values": {"crawl_max_pages": "lots"}}, headers=PANEL)).status_code == 422
    assert _row((await client.get("/api/settings/limits")).json(), "crawl_max_pages")["value"] == 25  # unchanged by the bad ones


# -- the workers use the saved values -----------------------------------------------------------------------------------------


async def test_a_crawl_uses_the_saved_page_limit_without_a_restart(fake, site_dir):  # noqa: F811
    await limits.put({"crawl_max_pages": 2})
    _, c1 = await _crawl(fake, site_dir)
    stored = await scrape_store.get(c1["id"])
    assert len(stored["items"]) == 2 and "Stopped at the page limit" in c1["result"]
    await limits.put({"crawl_max_pages": None, "crawl_max_depth": 0})
    _, c2 = await _crawl(fake, site_dir)
    assert len((await scrape_store.get(c2["id"]))["items"]) == 1  # only the start page


async def test_a_scrape_uses_the_saved_page_and_item_limits(fake, shop, local_site_is_fine):  # noqa: F811
    await limits.put({"scrape_max_pages": 1})
    _, _, s1 = await _scrape_task(fake, shop)
    stored = await scrape_store.get(s1["id"])
    assert stored["pages"] == 1 and len(stored["items"]) == 5 and stored["more_pages"] is True
    await limits.put({"scrape_max_pages": None, "scrape_max_items": 10})
    _, _, s2 = await _scrape_task(fake, shop)
    assert len((await scrape_store.get(s2["id"]))["items"]) == 10


async def test_the_daily_account_limit_is_the_saved_one():
    p = await db.pool()
    await p.execute("insert into site_accounts (domain, username, ciphertext, nonce, key_id, origin) values "
                    "('one.example.com', 'a', '\\x00', '\\x00', 'k', 'fleet')")
    ctx = RunContext(task={"id": None, "allow_registration": True}, subtask={"id": None}, dependency_results={}, worker_id="t")
    registration = Registration(ctx)
    await limits.put({"registrations_per_day": 1})
    assert "daily limit" in await registration._blocker("https://two.example.com/signup", "two.example.com")
    await limits.put({"registrations_per_day": 2})
    assert await registration._blocker("https://two.example.com/signup", "two.example.com") is None
    await limits.put({"registrations_per_day": 0})
    assert "0 accounts" in await registration._blocker("https://two.example.com/signup", "two.example.com")
