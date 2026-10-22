"""Structured scraping: the recipe logic, then the whole worker over a three-page test shop."""

import json
import pathlib

import httpx
import pytest

from fleet import automations, db, llm, models, scrape, scrape_store
from fleet.api import create_app
from fleet.config import settings
from fleet.fastbrowser import netguard

from .conftest import finished, new_task, running_fleet, sub, subtasks, wait_for

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "fast"
PANEL = {"X-Fleet-Client": "panel"}
REAL_GUARD = netguard.check_url_resolved  # captured before any test replaces it
BASE = "http://shop.test/cat/"

RECIPE = scrape.Recipe(
    item_selector="li.product",
    fields=[
        scrape.FieldRule(name="name", selector="a.name", attribute="text"),
        scrape.FieldRule(name="url", selector="a.name", attribute="href"),
        scrape.FieldRule(name="price", selector=".price", attribute="text"),
        scrape.FieldRule(name="rating", selector=".rating", attribute="data-score"),
    ],
    next_selector="a[rel=next]",
    notes="Products are li.product; the next link is a[rel=next].",
)


def page(n: int) -> str:
    return (FIXTURES / f"listing{n}.html").read_text(encoding="utf-8")


# -- the logic (no database, no network) ----------------------------------------------------------------


@pytest.mark.no_db
def test_extraction_reads_items_resolves_links_and_drops_empty_blocks():
    ex = scrape.extract(page(1), BASE + "listing1.html", RECIPE)
    assert ex.containers == 6 and len(ex.items) == 5 and ex.dropped_empty == 1  # the ad has no data
    first = ex.items[0]
    assert first == {"name": "Kingston Fury 16GB DDR4 3200", "url": BASE + "p/kingston-fury.html",
                     "price": "189,00 zł", "rating": "4.7"}
    assert scrape.judge(ex) is None


@pytest.mark.no_db
def test_a_recipe_is_only_trusted_when_it_really_finds_a_list():
    wrong_items = RECIPE.model_copy(update={"item_selector": "li.nothing"})
    assert "matched 0" in scrape.judge(scrape.extract(page(1), BASE, wrong_items))
    wrong_fields = RECIPE.model_copy(update={"fields": [
        scrape.FieldRule(name="a", selector=".gone", attribute="text"),
        scrape.FieldRule(name="b", selector=".nope", attribute="text")]})
    assert "empty" in scrape.judge(scrape.extract(page(1), BASE, wrong_fields))
    one_item = RECIPE.model_copy(update={"item_selector": "ul.products"})  # the container, not the items
    assert "at least 2" in scrape.judge(scrape.extract(page(1), BASE, one_item))
    # a selector the model got wrong must come back as feedback, never as an exception
    pseudo = RECIPE.model_copy(update={"item_selector": "li.product::text"})
    assert "plain CSS" in scrape.judge(scrape.extract(page(1), BASE, pseudo))


@pytest.mark.no_db
def test_next_page_links_stay_on_the_site_and_end_on_the_last_page():
    assert scrape.next_url(page(1), BASE + "listing1.html", RECIPE) == BASE + "listing2.html"
    assert scrape.next_url(page(3), BASE + "listing3.html", RECIPE) is None
    elsewhere = '<a rel="next" href="https://evil.example/steal">Next</a>'
    assert scrape.next_url(elsewhere, BASE, RECIPE) is None
    assert scrape.next_url('<a rel="next" href="#">Next</a>', BASE, RECIPE) is None
    assert scrape.next_url(page(1), BASE, RECIPE.model_copy(update={"next_selector": ""})) is None
    assert scrape.next_url(page(1), BASE, RECIPE.model_copy(update={"next_selector": "a::attr(href)"})) is None


@pytest.mark.no_db
def test_duplicates_across_pages_are_dropped():
    seen: set[tuple] = set()
    first = scrape.dedupe(scrape.extract(page(1), BASE + "listing1.html", RECIPE).items, seen)
    second = scrape.dedupe(scrape.extract(page(2), BASE + "listing1.html", RECIPE).items, seen)
    assert len(first) == 5 and len(second) == 4  # Kingston Fury is on both pages


@pytest.mark.no_db
def test_the_model_sees_a_small_digest_and_the_pagination_even_when_the_menu_is_trimmed():
    big = page(1).replace("</ul>", "".join(f'<li class="product"><a class="name" href="p/{i}.html">Item {i}</a>'
                                           f'<script>track({i})</script></li>' for i in range(3000)) + "</ul>")
    digest = scrape.digest(big)
    assert len(digest) <= scrape.DIGEST_CHARS and 'class="product"' in digest
    assert "<script" not in digest and "track(" not in digest
    assert "listing2.html" not in digest  # the pagination <nav> is trimmed from the digest ...
    hints = scrape.pagination_hints(big)
    assert any("listing2.html" in h for h in hints) and len(hints) <= 12  # ... but the model is told about it


@pytest.mark.no_db
def test_csv_cannot_run_scraped_text_as_a_spreadsheet_formula():
    rows = [{"name": '=HYPERLINK("http://evil.example","click")', "price": "-5", "note": "+48 600 100 200"},
            {"name": "@SUM(1+1)", "price": "189,00 zł", "note": "line one\nline two"}]
    text = scrape.to_csv(["name", "price", "note"], rows)
    assert text.startswith("﻿")  # Excel reads the Polish letters correctly
    lines = text.lstrip("﻿").splitlines()
    assert lines[1].startswith("\"'=HYPERLINK") and "'-5" in lines[1] and "'+48 600 100 200" in lines[1]
    assert lines[2].startswith("'@SUM") and "189,00 zł" in lines[2] and "line one line two" in lines[2]


@pytest.mark.no_db
def test_the_summary_is_a_short_table_with_coverage():
    names = ["name", "price"]
    rows = [{"name": f"item {i}|x", "price": "1"} for i in range(40)]
    text = scrape.summary(names, rows, pages=4, url="https://x.test/", fill={"name": 1.0, "price": 0.95}, reused=True,
                          more_pages=True)
    assert "Scraped 40 items from 4 pages" in text and "price 95%" in text and "saved recipe" in text
    assert "item 0/x" in text and "…and 25 more rows" in text and "page/item limit" in text
    assert len(text) < 3500


# -- the worker over a three-page shop -------------------------------------------------------------------


@pytest.fixture
def shop(fake, site, monkeypatch):
    """The real scrape worker; the model is a stand-in that answers with RECIPE and counts how often it's asked."""
    monkeypatch.setattr(settings(), "fetch_min_delay_seconds", 0)
    state = {"calls": 0, "users": [], "recipe": RECIPE, "site": site}

    async def structured(*, model, system, user, schema, usage, max_tokens=16000):
        state["calls"] += 1
        state["users"].append(user)
        return state["recipe"]

    monkeypatch.setattr(llm, "structured", structured)
    return state


@pytest.fixture
def local_site_is_fine(monkeypatch):
    async def public(url):
        return None

    monkeypatch.setattr(netguard, "check_url_resolved", public)


async def _scrape_task(fake, shop, path="listing1.html"):
    fake.plan.subtasks = [sub("s1", kind="scrape", url=f"{shop['site']}/{path}")]
    task = await new_task()
    async with running_fleet():
        t = await wait_for(lambda: finished(task["id"]), timeout=40)
    return task, t, (await subtasks(task["id"]))["s1"]


async def test_scrape_follows_the_pages_removes_duplicates_and_keeps_a_table(fake, shop, local_site_is_fine):
    task, t, s1 = await _scrape_task(fake, shop)
    assert t["status"] == "done" and s1["status"] == "done", s1["error"]
    assert "Scraped 12 items from 3 pages" in s1["result"] and "Coverage:" in s1["result"]
    stored = await scrape_store.get(s1["id"])
    assert stored["fields"] == ["name", "url", "price", "rating"] and len(stored["items"]) == 12
    assert len({tuple(i.values()) for i in stored["items"]}) == 12
    assert stored["pages"] == 3 and stored["reused"] is False and stored["more_pages"] is False
    assert any(i["name"].startswith("Mushkin") for i in stored["items"])  # page 3 was reached
    assert shop["calls"] == 1 and "listing2.html" in shop["users"][0]  # one model call, shown the pagination
    p = await db.pool()
    log = [r["message"] for r in await p.fetch("select message from events where message like 'Page %' order by id")]
    assert log == ["Page 1: 5 new items (5 total)", "Page 2: 4 new items (9 total)", "Page 3: 3 new items (12 total)"]
    assert float(s1["expected_cost"]) == 0.05


async def test_a_saved_automation_reads_the_pages_again_with_no_model_call(fake, shop, local_site_is_fine, monkeypatch):
    task, _, first = await _scrape_task(fake, shop)
    a = await automations.create_from_task(task["id"], "RAM prices", None, "UTC")
    assert a["steps"][0]["recipe"]["item_selector"] == "li.product"

    async def must_not_be_called(**kw):
        raise AssertionError("a saved recipe must not need the model")

    monkeypatch.setattr(llm, "structured", must_not_be_called)
    run = await automations.start_run(a["id"])
    replay = (await subtasks(run["id"]))["s1"]
    assert replay["kind"] == "scrape" and replay["guide"]["item_selector"] == "li.product"
    async with running_fleet():
        await wait_for(lambda: finished(run["id"]), timeout=40)
    again = (await subtasks(run["id"]))["s1"]
    assert again["status"] == "done", again["error"]
    stored = await scrape_store.get(again["id"])
    assert len(stored["items"]) == 12 and stored["reused"] is True
    assert "Used the saved recipe" in again["result"]


async def test_a_recipe_that_stops_fitting_is_learned_again(fake, shop, local_site_is_fine):
    task, _, _ = await _scrape_task(fake, shop)
    a = await automations.create_from_task(task["id"], "RAM prices", None, "UTC")
    p = await db.pool()
    broken = {**RECIPE.model_dump(), "item_selector": "li.old-layout"}
    steps = a["steps"]
    steps[0]["recipe"] = broken
    await p.execute("update automations set steps = $2 where id = $1", a["id"], steps)
    shop["calls"] = 0
    run = await automations.start_run(a["id"])
    async with running_fleet():
        await wait_for(lambda: finished(run["id"]), timeout=40)
    again = (await subtasks(run["id"]))["s1"]
    assert again["status"] == "done" and shop["calls"] == 1  # the site "changed": learned once more
    assert (await scrape_store.get(again["id"]))["reused"] is False
    warn = await p.fetchval("select count(*) from events where message like '%no longer fits%'")
    assert warn == 1


async def test_page_and_item_limits_hold(fake, shop, local_site_is_fine, monkeypatch):
    monkeypatch.setattr(settings(), "scrape_max_pages", 2)
    _, _, s1 = await _scrape_task(fake, shop)
    stored = await scrape_store.get(s1["id"])
    assert stored["pages"] == 2 and len(stored["items"]) == 9 and stored["more_pages"] is True
    assert "page/item limit" in s1["result"]
    monkeypatch.setattr(settings(), "scrape_max_pages", 10)
    monkeypatch.setattr(settings(), "scrape_max_items", 7)
    _, _, s1 = await _scrape_task(fake, shop)
    stored = await scrape_store.get(s1["id"])
    assert len(stored["items"]) == 7 and stored["more_pages"] is True


async def test_a_page_without_a_list_fails_clearly_after_one_repair_round(fake, shop, local_site_is_fine):
    _, t, s1 = await _scrape_task(fake, shop, path="article.html")
    assert s1["status"] == "failed" and "Couldn't find a repeating list" in s1["error"] and "browser step" in s1["error"]
    assert shop["calls"] == 2 and "was rejected" in shop["users"][1]  # the repair round told the model why
    assert await models.model_for("smart")  # (the recipe model is the 'smart' role)


async def test_scrape_obeys_robots_and_never_goes_to_local_addresses(fake, shop, local_site_is_fine, monkeypatch):
    _, _, s1 = await _scrape_task(fake, shop, path="private/secret.html")
    assert s1["status"] == "failed" and "robots.txt" in s1["error"] and shop["calls"] == 0

    monkeypatch.setattr(netguard, "check_url_resolved", REAL_GUARD)  # the real guard: 127.0.0.1 isn't public
    _, _, s1 = await _scrape_task(fake, shop)
    assert s1["status"] == "failed" and "public web pages" in s1["error"]


async def test_a_scrape_step_needs_a_start_url(fake, shop):
    fake.plan.subtasks = [sub("s1", kind="scrape", url=None)]
    task = await new_task()
    async with running_fleet():
        await wait_for(lambda: finished(task["id"]), timeout=30)
    s1 = (await subtasks(task["id"]))["s1"]
    assert s1["status"] == "failed" and "without a URL" in s1["error"]


# -- the API: view and download --------------------------------------------------------------------------


@pytest.fixture
async def client():
    app = create_app(run_background=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8710") as c:
        yield c


async def test_scraped_data_can_be_viewed_and_downloaded(client, fake, shop, local_site_is_fine, monkeypatch):
    task, _, s1 = await _scrape_task(fake, shop)
    r = await client.get(f"/api/subtasks/{s1['id']}/data?limit=5&offset=10")
    body = r.json()
    assert r.status_code == 200 and body["count"] == 12 and len(body["items"]) == 2 and body["fields"][0] == "name"
    csv_r = await client.get(f"/api/subtasks/{s1['id']}/export.csv")
    assert csv_r.status_code == 200 and csv_r.headers["content-type"].startswith("text/csv")
    assert "attachment" in csv_r.headers["content-disposition"] and ".csv" in csv_r.headers["content-disposition"]
    assert csv_r.text.startswith("﻿name,url,price,rating") and len(csv_r.text.splitlines()) == 13
    json_r = await client.get(f"/api/subtasks/{s1['id']}/export.json")
    assert json.loads(json_r.text)["count"] == 12
    detail = (await client.get(f"/api/tasks/{task['id']}")).json()
    assert detail["subtasks"][0]["data_count"] == 12
    assert (await client.get("/api/subtasks/00000000-0000-0000-0000-000000000000/data")).status_code == 404
    # downloads are protected by the access token like everything else (the panel passes it as ?token=)
    monkeypatch.setattr(settings(), "api_token", "s3cret")
    assert (await client.get(f"/api/subtasks/{s1['id']}/export.csv")).status_code == 401
    assert (await client.get(f"/api/subtasks/{s1['id']}/export.csv?token=s3cret")).status_code == 200


async def test_pages_collected_before_a_block_are_kept(fake, shop, local_site_is_fine, monkeypatch):
    """Page 2 gets blocked: the 5 items from page 1 must survive, with the reason, not a failed step."""
    from fleet import webfetch
    from fleet.executors import Blocked

    real_get = webfetch.get
    calls = {"n": 0}

    async def flaky(url, **kw):
        calls["n"] += 1
        if calls["n"] == 2:
            raise Blocked(f"{url} answered HTTP 429: likely needs a real browser")
        return await real_get(url, **kw)

    monkeypatch.setattr(webfetch, "get", flaky)
    _, t, s1 = await _scrape_task(fake, shop)
    assert s1["status"] == "done", s1["error"]
    stored = await scrape_store.get(s1["id"])
    assert len(stored["items"]) == 5 and stored["more_pages"] is True
    assert "Stopped early: page 2 couldn't be read" in s1["result"] and "HTTP 429" in s1["result"]


async def test_a_javascript_shell_fails_before_any_model_call(fake, shop, local_site_is_fine, tmp_path, monkeypatch):
    from fleet import webfetch

    async def shell(url, **kw):
        return webfetch.Fetched(url=url, status=200, html='<html><body><div id="root"></div><script src="app.js"></script></body></html>')

    monkeypatch.setattr(webfetch, "get", shell)
    _, _, s1 = await _scrape_task(fake, shop)
    assert s1["status"] == "failed" and "built by JavaScript" in s1["error"] and shop["calls"] == 0


@pytest.mark.no_db
def test_empty_shells_are_recognised_and_real_pages_are_not():
    assert scrape.looks_empty('<html><body><div id="root"></div></body></html>')
    assert not scrape.looks_empty(page(1))


@pytest.mark.no_db
async def test_a_huge_page_is_refused_not_parsed(monkeypatch):
    from fleet import webfetch
    from fleet.executors import Blocked

    async def public(url):
        return None

    class Huge:
        status, url, body = 200, "https://big.test/", b""
        html_content = "x" * (webfetch.MAX_HTML_CHARS + 1)

    async def fake_get(url, **kw):
        return Huge()

    monkeypatch.setattr(netguard, "check_url_resolved", public)
    monkeypatch.setattr(settings(), "fetch_min_delay_seconds", 0)
    monkeypatch.setattr(webfetch.AsyncFetcher, "get", staticmethod(fake_get))
    with pytest.raises(Blocked, match="too large"):
        await webfetch.get("https://big.test/")
