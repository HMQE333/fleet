"""Scraping item pages: when the goal needs more than the list shows, Fleet opens each item's own page (same site,
polite, capped) and adds what it shows as extra columns.

The rules under test: the model learns the item-page recipe once (from one real sample, checked by code); saved
automations reuse it for free; the same-site / robots / pacing rules of the list scrape apply to every item page;
a step that can't read item pages keeps the list data; a hard cap on pages per step.
"""

import functools
import http.server
import re
import shutil
import threading

import pytest

from fleet import automations, db, llm, scrape, scrape_store, webfetch
from fleet.config import settings
from fleet.executors import Blocked
from fleet.fastbrowser import netguard

from .conftest import finished, new_task, running_fleet, sub, subtasks, wait_for
from .test_scrape import FIXTURES, RECIPE

LIST_RECIPE = RECIPE.model_copy(update={"detail_link_field": "url", "detail_wanted": ["sku", "stock", "description"]})
DETAIL_RECIPE = scrape.DetailRecipe(
    fields=[scrape.FieldRule(name="sku", selector=".sku", attribute="text"),
            scrape.FieldRule(name="stock", selector=".stock", attribute="text"),
            scrape.FieldRule(name="description", selector="p.desc", attribute="text")],
    notes="sku, stock and description have their own classes")
WRONG_DETAIL = scrape.DetailRecipe(
    fields=[scrape.FieldRule(name="sku", selector=".nope", attribute="text"),
            scrape.FieldRule(name="stock", selector=".nothing", attribute="text")], notes="wrong on purpose")
DETAIL_PAGE = """<!doctype html><html><head><title>{slug}</title></head><body>
<nav>Menu <a href="../listing1.html">Home</a> <a href="../signin.html">Log in</a></nav>
<h1 class="title">{slug}</h1>
<span class="sku">SKU-{slug}</span>
<div class="stock">In stock: {n} pcs</div>
<p class="desc">A good kit, called {slug}.</p>
<aside class="related">Related: a different product with its own SKU-other</aside>
</body></html>"""


# -- the pure helpers -------------------------------------------------------------------------------------------


@pytest.mark.no_db
def test_item_links_must_be_http_on_the_same_site_and_not_the_list_itself():
    base = "https://www.shop.test/cat/list.html"
    assert scrape.detail_url("p/a.html", base) == "https://www.shop.test/cat/p/a.html"
    assert scrape.detail_url("https://shop.test/p/b#top", base) == "https://shop.test/p/b"  # www and fragment don't matter
    for bad in ("https://other.example/p/a", "//evil.test/x", "javascript:alert(1)", "mailto:a@b.c", "", "  ", None, 5,
                "list.html#reviews", "ftp://shop.test/a"):
        assert scrape.detail_url(bad, base) is None, bad


@pytest.mark.no_db
def test_detail_columns_never_clash_with_the_lists_columns():
    assert scrape.detail_columns(["name", "price"], ["sku", "price", "name", "price"]) == [
        "sku", "detail_price", "detail_name", "detail_price_"]


@pytest.mark.no_db
def test_an_item_page_recipe_is_only_trusted_when_it_really_reads_the_page():
    page = DETAIL_PAGE.format(slug="kingston", n=4)
    values, err = scrape.extract_detail(page, "https://shop.test/p/k.html", DETAIL_RECIPE)
    assert err is None and values == {"sku": "SKU-kingston", "stock": "In stock: 4 pcs", "description": "A good kit, called kingston."}
    assert scrape.judge_detail(values, err) is None
    blank, err = scrape.extract_detail(page, "https://shop.test/p/k.html", WRONG_DETAIL)
    assert "Only 0 of 2 fields" in scrape.judge_detail(blank, err)
    half = {"a": "x", "b": None, "c": None}
    assert "Only 1 of 3" in scrape.judge_detail(half, None)
    bad_selector = scrape.DetailRecipe(fields=[scrape.FieldRule(name="x", selector="div::text", attribute="text")], notes="")
    values, err = scrape.extract_detail(page, "https://shop.test/", bad_selector)
    assert "couldn't be used" in scrape.judge_detail(values, err)


# -- the worker over a shop whose items have their own pages --------------------------------------------------------


@pytest.fixture
def shop(fake, tmp_path, monkeypatch):
    root = tmp_path / "shop"
    (root / "p").mkdir(parents=True)
    for n in (1, 2, 3):
        shutil.copy(FIXTURES / f"listing{n}.html", root / f"listing{n}.html")
    page2 = (root / "listing2.html").read_text(encoding="utf-8")  # the fixture repeats one product on purpose
    (root / "listing2.html").write_text("\n".join(ln for ln in page2.splitlines() if "kingston-fury" not in ln), encoding="utf-8")
    slugs = sorted({m for n in (1, 2, 3) for m in re.findall(r'href="p/([^"]+)\.html"', (root / f"listing{n}.html").read_text(encoding="utf-8"))})
    for i, slug in enumerate(slugs):
        (root / "p" / f"{slug}.html").write_text(DETAIL_PAGE.format(slug=slug, n=i + 1), encoding="utf-8")
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(root))
    handler.log_message = lambda *a: None
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    state = {"list": LIST_RECIPE, "detail": [DETAIL_RECIPE], "calls": [], "fetched": [], "slugs": slugs, "root": root,
             "url": f"http://127.0.0.1:{server.server_address[1]}/listing1.html"}

    async def public(url):
        return None

    async def structured(*, model, system, user, schema, usage, max_tokens=16000):
        state["calls"].append(schema.__name__)
        if schema is scrape.DetailRecipe:
            return state["detail"].pop(0) if len(state["detail"]) > 1 else state["detail"][0]
        return state["list"]

    real_get = webfetch.get

    async def recording_get(url, **kw):
        state["fetched"].append(url)
        return await real_get(url, **kw)

    monkeypatch.setattr(netguard, "check_url_resolved", public)
    monkeypatch.setattr(llm, "structured", structured)
    monkeypatch.setattr(webfetch, "get", recording_get)
    monkeypatch.setattr(settings(), "fetch_min_delay_seconds", 0)
    yield state
    server.shutdown()


def _item_fetches(shop) -> list[str]:
    return [u for u in shop["fetched"] if "/p/" in u]


async def _scrape(fake, shop):
    fake.plan.subtasks = [sub("s1", kind="scrape", url=shop["url"])]
    task = await new_task()
    async with running_fleet():
        await wait_for(lambda: finished(task["id"]), timeout=90)
    return task, (await subtasks(task["id"]))["s1"]


async def test_item_pages_add_columns_and_the_model_is_asked_once_for_each_kind(fake, shop):
    _, s1 = await _scrape(fake, shop)
    assert s1["status"] == "done", s1["error"]
    stored = await scrape_store.get(s1["id"])
    assert stored["fields"] == ["name", "url", "price", "rating", "sku", "stock", "description"]
    assert len(stored["items"]) == 12 and len(_item_fetches(shop)) == 12
    for item in stored["items"]:
        slug = item["url"].rsplit("/", 1)[1].removesuffix(".html")
        assert item["sku"] == f"SKU-{slug}" and item["stock"].startswith("In stock:") and slug in item["description"]
    assert shop["calls"] == ["ListRecipe", "DetailRecipe"]  # one call to learn the list, one to learn an item page
    assert stored["recipe"]["detail"]["fields"][0]["name"] == "sku"
    assert "Opened 12 item pages of 12 for sku, stock, description." in s1["result"]
    assert "Fields: name, url, price, rating, sku, stock, description." in s1["result"]


async def test_a_hard_cap_limits_the_item_pages_per_step(fake, shop, monkeypatch):
    monkeypatch.setattr(settings(), "scrape_max_details", 5)
    _, s1 = await _scrape(fake, shop)
    stored = await scrape_store.get(s1["id"])
    assert len(_item_fetches(shop)) == 5 and sum(i["sku"] is not None for i in stored["items"]) == 5
    assert sum(i["sku"] is None for i in stored["items"]) == 7 and len(stored["items"]) == 12
    assert "Opened 5 item pages of 12" in s1["result"] and "the limit is 5 per step" in s1["result"]


async def test_a_saved_automation_reads_the_item_pages_again_with_no_model_call(fake, shop, monkeypatch):
    task, _ = await _scrape(fake, shop)
    a = await automations.create_from_task(task["id"], "RAM", None, "UTC")
    assert a["steps"][0]["recipe"]["detail"]["fields"][2]["name"] == "description"

    async def must_not_be_called(**kw):
        raise AssertionError("a saved recipe must not need the model")

    monkeypatch.setattr(llm, "structured", must_not_be_called)
    shop["fetched"].clear()
    run = await automations.start_run(a["id"])
    async with running_fleet():
        await wait_for(lambda: finished(run["id"]), timeout=90)
    again = (await subtasks(run["id"]))["s1"]
    assert again["status"] == "done", again["error"]
    stored = await scrape_store.get(again["id"])
    assert stored["reused"] is True and all(i["sku"] for i in stored["items"]) and len(_item_fetches(shop)) == 12


async def test_an_item_page_recipe_that_stops_fitting_is_learned_again(fake, shop, monkeypatch):
    task, _ = await _scrape(fake, shop)
    a = await automations.create_from_task(task["id"], "RAM", None, "UTC")
    p = await db.pool()
    steps = a["steps"]
    steps[0]["recipe"]["detail"] = WRONG_DETAIL.model_dump()  # the site "changed" its item pages
    await p.execute("update automations set steps = $2 where id = $1", a["id"], steps)
    shop["calls"].clear()
    run = await automations.start_run(a["id"])
    async with running_fleet():
        await wait_for(lambda: finished(run["id"]), timeout=90)
    again = (await subtasks(run["id"]))["s1"]
    stored = await scrape_store.get(again["id"])
    assert again["status"] == "done" and shop["calls"] == ["DetailRecipe"]  # the list recipe was reused, the item one relearned
    assert all(i["sku"] for i in stored["items"])
    assert await p.fetchval("select count(*) from events where message like '%item-page recipe no longer fits%'") == 1


async def test_a_bad_item_page_recipe_gets_one_repair_round_then_the_list_data_is_kept(fake, shop):
    shop["detail"] = [WRONG_DETAIL]
    _, s1 = await _scrape(fake, shop)
    assert s1["status"] == "done", s1["error"]
    stored = await scrape_store.get(s1["id"])
    assert shop["calls"] == ["ListRecipe", "DetailRecipe", "DetailRecipe"]
    assert stored["fields"] == ["name", "url", "price", "rating"] and len(stored["items"]) == 12
    assert "couldn't work out how to read an item page" in s1["result"]
    assert len(_item_fetches(shop)) == 1  # it never kept opening pages it couldn't read


async def test_a_site_that_refuses_the_item_pages_is_left_alone_after_three_refusals(fake, shop, monkeypatch):
    real = webfetch.get

    async def refusing(url, **kw):
        if "/p/" in url:
            shop["fetched"].append(url)
            raise Blocked(f"{url} answered HTTP 429: likely needs a real browser")
        return await real(url, **kw)

    monkeypatch.setattr(webfetch, "get", refusing)
    _, s1 = await _scrape(fake, shop)
    assert s1["status"] == "done", s1["error"]
    assert len(_item_fetches(shop)) == 3  # polite: it stopped asking
    assert "the site keeps refusing the item pages" in s1["result"]
    assert len((await scrape_store.get(s1["id"]))["items"]) == 12


async def test_links_to_other_sites_are_never_followed(fake, shop):
    (shop["root"] / "listing1.html").write_text(
        (shop["root"] / "listing1.html").read_text(encoding="utf-8").replace(
            'href="p/kingston-fury.html"', 'href="https://evil.example.net/steal.html"'), encoding="utf-8")
    _, s1 = await _scrape(fake, shop)
    stored = await scrape_store.get(s1["id"])
    assert not any("evil.example.net" in u for u in shop["fetched"]) and len(_item_fetches(shop)) == 11
    off_site = next(i for i in stored["items"] if "evil.example.net" in (i["url"] or ""))
    assert off_site["sku"] is None  # the list data is kept, the extra columns stay empty
    assert "Opened 11 item pages of 11" in s1["result"]


async def test_a_link_field_that_does_not_exist_means_no_item_pages(fake, shop):
    shop["list"] = LIST_RECIPE.model_copy(update={"detail_link_field": "link"})
    _, s1 = await _scrape(fake, shop)
    stored = await scrape_store.get(s1["id"])
    assert _item_fetches(shop) == [] and stored["fields"] == ["name", "url", "price", "rating"]
    assert shop["calls"] == ["ListRecipe"] and stored["recipe"]["detail_link_field"] == ""


async def test_a_goal_that_needs_nothing_more_never_opens_item_pages(fake, shop):
    shop["list"] = RECIPE
    _, s1 = await _scrape(fake, shop)
    assert s1["status"] == "done" and _item_fetches(shop) == [] and shop["calls"] == ["ListRecipe"]


async def test_incomplete_item_pages_make_the_change_report_cautious(fake, shop, monkeypatch):
    task, _ = await _scrape(fake, shop)
    a = await automations.create_from_task(task["id"], "RAM", None, "UTC")
    monkeypatch.setattr(settings(), "scrape_max_details", 4)
    run = await automations.start_run(a["id"])
    async with running_fleet():
        await wait_for(lambda: finished(run["id"]), timeout=90)
    again = (await subtasks(run["id"]))["s1"]
    changes = (await scrape_store.get(again["id"]))["changes"]
    # eight rows lost their item-page values only because the cap is lower: that is not "eight rows changed"
    assert changes["partial"] is True and changes["counts"]["changed"] == 0
