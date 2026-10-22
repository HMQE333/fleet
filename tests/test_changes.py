"""Change monitoring: what is new, removed or edited between two scrapes of the same page.

The logic first (no database), then a saved automation scraping a test shop whose prices and listings change
between runs.
"""

import http.server
import functools
import pathlib
import shutil
import threading

import httpx
import pytest

from fleet import automations, changes, db, llm, scrape_store
from fleet.api import create_app
from fleet.config import settings
from fleet.fastbrowser import netguard

from .conftest import finished, new_task, running_fleet, sub, subtasks, wait_for
from .test_scrape import FIXTURES, RECIPE

FIELDS = ["name", "url", "price"]


def row(name, price, url=None):
    return {"name": name, "url": url or f"/p/{name.lower().replace(' ', '-')}", "price": price}


BEFORE = [row("Kingston", "189 zł"), row("Corsair", "215 zł"), row("Crucial", "259 zł")]


# -- the logic ----------------------------------------------------------------------------------------------


@pytest.mark.no_db
def test_new_removed_and_edited_rows_are_found_by_the_url():
    after = [row("Kingston", "175 zł"), row("Crucial", "259 zł"), row("Patriot", "169 zł")]
    d = changes.diff(FIELDS, BEFORE, FIELDS, after)
    assert d["comparable"] and d["key"] == "url" and d["partial"] is False
    assert d["counts"] == {"added": 1, "removed": 1, "changed": 1, "unchanged": 1}
    assert [r["name"] for r in d["added"]] == ["Patriot"] and [r["name"] for r in d["removed"]] == ["Corsair"]
    (edit,) = d["changed"]
    assert edit["label"] == "Kingston" and edit["changes"] == {"price": {"from": "189 zł", "to": "175 zł"}}
    assert d["fields_changed"] == {"price": 1}


@pytest.mark.no_db
def test_nothing_changed_and_whitespace_is_not_a_change():
    same = [row("Kingston", "189  zł"), row("Corsair", " 215 zł"), row("Crucial", "259 zł")]
    d = changes.diff(FIELDS, BEFORE, FIELDS, same)
    assert d["counts"] == {"added": 0, "removed": 0, "changed": 0, "unchanged": 3}


@pytest.mark.no_db
def test_ids_and_links_are_preferred_as_the_key_and_unusable_columns_are_skipped():
    assert changes.pick_key(FIELDS, BEFORE, BEFORE) == "url"  # a unique name would also do, a link is steadier
    dup_urls = [{**r, "url": "/same"} for r in BEFORE]
    assert changes.pick_key(FIELDS, dup_urls, dup_urls) == "name"  # duplicates can't identify a row
    gap = [BEFORE[0], {**BEFORE[1], "url": ""}, BEFORE[2]]
    assert changes.pick_key(FIELDS, gap, BEFORE) == "name"  # an empty cell in either run rules a column out
    same_everything = [{"a": "x", "b": "1"}, {"a": "x", "b": "1"}]
    assert changes.pick_key(["a", "b"], same_everything, same_everything) is None


@pytest.mark.no_db
def test_without_a_unique_column_an_edit_is_one_removed_and_one_added():
    old = [{"city": "Gdańsk", "temp": "12"}, {"city": "Gdańsk", "temp": "12"}, {"city": "Łódź", "temp": "9"}]
    new = [{"city": "Gdańsk", "temp": "12"}, {"city": "Gdańsk", "temp": "12"}, {"city": "Łódź", "temp": "11"}]
    d = changes.diff(["city", "temp"], old, ["city", "temp"], new)
    assert d["key"] is None and d["counts"] == {"added": 1, "removed": 1, "changed": 0, "unchanged": 2}
    assert d["added"] == [{"city": "Łódź", "temp": "11"}] and d["removed"] == [{"city": "Łódź", "temp": "9"}]
    again = new + [{"city": "Gdańsk", "temp": "12"}]  # a third identical row counts too (multiset, not set)
    assert changes.diff(["city", "temp"], new, ["city", "temp"], again)["counts"]["added"] == 1


@pytest.mark.no_db
def test_an_incomplete_run_never_claims_rows_appeared_or_vanished():
    after = [row("Kingston", "175 zł"), row("Patriot", "169 zł")]  # the scrape stopped early
    d = changes.diff(FIELDS, BEFORE, FIELDS, after, partial=True)
    assert d["partial"] and d["counts"]["added"] == 0 and d["counts"]["removed"] == 0
    assert d["counts"]["changed"] == 1  # the edit to a row seen in both runs is still real
    text = changes.describe(d, FIELDS, "2026-10-02 08:00 UTC")
    assert "stopped early" in text and "Kingston" in text and "Patriot" not in text


@pytest.mark.no_db
def test_different_columns_are_not_compared():
    d = changes.diff(FIELDS, BEFORE, ["title", "link"], [{"title": "x", "link": "y"}])
    assert d["comparable"] is False and "columns are different" in d["reason"]
    assert "can't be compared" in changes.describe(d, ["title", "link"], "yesterday")


@pytest.mark.no_db
def test_the_same_columns_in_another_order_still_compare_and_long_values_are_cut_in_the_report():
    shuffled = ["price", "url", "name"]
    after = [row("Kingston", "x" * 500), row("Corsair", "215 zł"), row("Crucial", "259 zł")]
    d = changes.diff(FIELDS, BEFORE, shuffled, after)
    assert d["comparable"] and d["counts"]["changed"] == 1
    line = next(ln for ln in changes.describe(d, shuffled, "x").splitlines() if ln.startswith("~"))
    assert len(line) < 260 and "x" * 81 not in line


@pytest.mark.no_db
def test_columns_that_were_only_partly_read_ignore_edits_to_or_from_nothing():
    before = [{**row("Kingston", "189 zł"), "sku": "K-1"}, {**row("Corsair", "215 zł"), "sku": "C-1"}]
    after = [{**row("Kingston", "189 zł"), "sku": None}, {**row("Corsair", "215 zł"), "sku": "C-2"}]
    fields = [*FIELDS, "sku"]
    soft = changes.diff(fields, before, fields, after, partial=True, soft_fields={"sku"})
    assert soft["counts"]["changed"] == 1 and soft["changed"][0]["changes"] == {"sku": {"from": "C-1", "to": "C-2"}}
    plain = changes.diff(fields, before, fields, after, partial=True)
    assert plain["counts"]["changed"] == 2  # without the hint, losing a value counts as an edit


@pytest.mark.no_db
def test_big_changes_keep_exact_counts_but_short_lists():
    many = [row(f"Item {i}", "1 zł") for i in range(120)]
    d = changes.diff(FIELDS, [], FIELDS, many)
    assert d["counts"]["added"] == 120 and len(d["added"]) == changes.LIST_CAP and d["truncated"]
    assert changes.describe(d, FIELDS, "x").count("\n+ Item") == 8  # the text shows a handful, then "and N more"
    assert "…and 112 more" in changes.describe(d, FIELDS, "x")


@pytest.mark.no_db
def test_the_report_reads_like_a_summary():
    after = [row("Kingston", "175 zł"), row("Crucial", "259 zł"), row("Patriot", "169 zł")]
    text = changes.describe(changes.diff(FIELDS, BEFORE, FIELDS, after), FIELDS, "2026-10-02 08:00 UTC")
    assert text.splitlines()[0] == ("Changes since the previous run (2026-10-02 08:00 UTC): 1 new, 1 removed, "
                                    "1 changed, 1 unchanged.")
    assert "+ Patriot" in text and "- Corsair" in text and "~ Kingston: price: 189 zł → 175 zł" in text
    none = changes.describe(changes.diff(FIELDS, BEFORE, FIELDS, BEFORE), FIELDS, "x")
    assert none.startswith("No changes since the previous run (x): 3 rows are the same")


# -- a saved automation watching a shop whose prices move ---------------------------------------------------


@pytest.fixture
def shop(fake, tmp_path, monkeypatch):
    """A three-page test shop (12 distinct products) in a temp folder so a test can edit it between runs."""
    root = tmp_path / "shop"
    root.mkdir()
    for n in (1, 2, 3):
        shutil.copy(FIXTURES / f"listing{n}.html", root / f"listing{n}.html")
    page2 = (root / "listing2.html").read_text(encoding="utf-8")  # the fixture repeats one product on purpose
    kept = [line for line in page2.splitlines() if "kingston-fury" not in line]
    (root / "listing2.html").write_text("\n".join(kept), encoding="utf-8")
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(root))
    handler.log_message = lambda *a: None
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    async def public(url):
        return None

    async def structured(*, model, system, user, schema, usage, max_tokens=16000):
        return RECIPE

    monkeypatch.setattr(netguard, "check_url_resolved", public)
    monkeypatch.setattr(llm, "structured", structured)
    monkeypatch.setattr(settings(), "fetch_min_delay_seconds", 0)

    def edit(page: str, old: str, new: str):
        path = pathlib.Path(root / page)
        text = path.read_text(encoding="utf-8")
        assert old in text, old
        path.write_text(text.replace(old, new), encoding="utf-8")

    yield type("Shop", (), {"url": f"http://127.0.0.1:{server.server_address[1]}/listing1.html", "edit": staticmethod(edit)})
    server.shutdown()


async def _watch(fake, shop):
    """The operator's first run (a plain task), saved as an automation."""
    fake.plan.subtasks = [sub("s1", kind="scrape", url=shop.url)]
    task = await new_task(prompt="Watch the RAM prices")
    async with running_fleet():
        await wait_for(lambda: finished(task["id"]), timeout=40)
    first = (await subtasks(task["id"]))["s1"]
    return first, await automations.create_from_task(task["id"], "RAM prices", None, "UTC")


async def _run(a):
    run = await automations.start_run(a["id"])
    async with running_fleet():
        await wait_for(lambda: finished(run["id"]), timeout=40)
    return run, (await subtasks(run["id"]))["s1"]


async def test_a_plain_task_has_nothing_to_compare_with(fake, shop):
    first, _ = await _watch(fake, shop)
    assert first["status"] == "done" and "Changes since" not in first["result"]
    assert (await scrape_store.get(first["id"]))["changes"] is None


async def test_the_first_automation_run_is_compared_with_the_run_it_was_saved_from(fake, shop):
    _, a = await _watch(fake, shop)
    shop.edit("listing1.html", "189,00 zł", "175,00 zł")  # a price drop
    shop.edit("listing1.html", '  <li class="product"><a class="name" href="p/patriot-viper.html">Patriot Viper 16GB DDR4 3200</a> <span class="price">169,00 zł</span> <span class="rating" data-score="4.2">4.2/5</span></li>\n', "")
    shop.edit("listing3.html", "</ul>", '<li class="product"><a class="name" href="p/new-kit.html">Brand New 16GB Kit</a> <span class="price">99,00 zł</span> <span class="rating" data-score="4.9">4.9/5</span></li></ul>')
    run, s1 = await _run(a)
    assert s1["status"] == "done", s1["error"]
    stored = await scrape_store.get(s1["id"])
    c = stored["changes"]
    assert c["counts"] == {"added": 1, "removed": 1, "changed": 1, "unchanged": 10}
    assert [r["name"] for r in c["added"]] == ["Brand New 16GB Kit"]
    assert [r["name"] for r in c["removed"]] == ["Patriot Viper 16GB DDR4 3200"]
    assert c["changed"][0]["changes"]["price"] == {"from": "189,00 zł", "to": "175,00 zł"}
    lines = s1["result"].splitlines()
    assert lines[0].startswith("Changes since the previous run (") and "1 new, 1 removed, 1 changed" in lines[0]
    assert "+ Brand New 16GB Kit" in s1["result"] and "- Patriot Viper 16GB DDR4 3200" in s1["result"]
    assert "Scraped 12 items" in s1["result"]  # the usual summary is still there
    p = await db.pool()
    logged = await p.fetchrow("select message, data from events where type = 'scrape.changes'")
    assert logged["message"] == "1 new, 1 removed, 1 changed since the previous run"


async def test_the_next_run_is_compared_with_the_one_before_it_not_with_the_original(fake, shop):
    _, a = await _watch(fake, shop)
    shop.edit("listing1.html", "189,00 zł", "175,00 zł")
    await _run(a)
    _, s1 = await _run(a)  # nothing moved since the last run
    assert s1["result"].startswith("No changes since the previous run (")
    assert (await scrape_store.get(s1["id"]))["changes"]["counts"] == {"added": 0, "removed": 0, "changed": 0, "unchanged": 12}


async def test_a_run_that_stopped_early_reports_edits_but_not_missing_rows(fake, shop, monkeypatch):
    _, a = await _watch(fake, shop)
    shop.edit("listing1.html", "189,00 zł", "175,00 zł")
    monkeypatch.setattr(settings(), "scrape_max_pages", 1)  # only page 1 is read this time
    _, s1 = await _run(a)
    c = (await scrape_store.get(s1["id"]))["changes"]
    assert c["partial"] and c["counts"]["removed"] == 0 and c["counts"]["changed"] == 1
    assert "stopped early" in s1["result"]


async def test_a_changed_layout_with_new_columns_is_reported_as_not_comparable(fake, shop):
    _, a = await _watch(fake, shop)
    p = await db.pool()
    steps = a["steps"]
    steps[0]["recipe"] = {**steps[0]["recipe"], "fields": [{**f, "name": "title" if f["name"] == "name" else f["name"]}
                                                           for f in steps[0]["recipe"]["fields"]]}
    await p.execute("update automations set steps = $2 where id = $1", a["id"], steps)
    _, s1 = await _run(a)
    assert s1["status"] == "done", s1["error"]
    c = (await scrape_store.get(s1["id"]))["changes"]
    assert c["comparable"] is False and "columns are different" in s1["result"]
    assert await p.fetchval("select count(*) from events where type = 'scrape.changes'") == 0


@pytest.fixture
async def client():
    app = create_app(run_background=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8710") as c:
        yield c


async def test_the_panel_api_shows_the_counts_and_the_lists(client, fake, shop):
    _, a = await _watch(fake, shop)
    shop.edit("listing1.html", "189,00 zł", "175,00 zł")
    run, s1 = await _run(a)
    detail = (await client.get(f"/api/tasks/{run['id']}")).json()
    assert detail["subtasks"][0]["data_changes"] == {"added": 0, "removed": 0, "changed": 1, "unchanged": 11}
    data = (await client.get(f"/api/subtasks/{s1['id']}/data?limit=1")).json()
    assert data["changes"]["changed"][0]["label"].startswith("Kingston")
    assert data["changes"]["changed"][0]["changes"]["price"]["to"] == "175,00 zł"
