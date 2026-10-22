"""Watches: "tell me when <column> crosses <number>" on a saved scrape.

The rules under test: numbers are read from cell text by code in every local format; only rows that JUST crossed the line are alerts
(not the same cheap row every run); rules are checked against the automation's real scrape steps and columns; alerts reach the result
text and the Updates feed; plain tasks and other steps never alert.
"""

import httpx
import pytest

from fleet import automations, db, scrape_store, watches
from fleet.api import create_app

from .conftest import subtasks
from .test_changes import _run, _watch, shop  # noqa: F401  (the test shop and its helpers are shared)

PANEL = {"X-Fleet-Client": "panel"}


@pytest.mark.no_db
@pytest.mark.parametrize("text,expected", [
    ("189,00 zł", 189.0), ("1 299,99 zł", 1299.99), ("1.299,99 zł", 1299.99), ("$1,299.99", 1299.99), ("12.50 EUR", 12.5),
    ("1299", 1299.0), ("1,299", 1299.0), ("1.299 zł", 1299.0), ("0.125", 0.125), ("-5,5", -5.5), ("9 999,99", 9999.99),
    ("1 299,00 zł", 1299.0), ("cena: 15 szt. 20", 15.0), ("100-200 zł", 100.0), ("1,234,567", 1234567.0),
    ("3.5.2020", None), ("brak", None), ("", None), (None, None), ("zł", None), ("1,2,3", None),
])
def test_numbers_are_read_from_cell_text_in_any_local_format(text, expected):
    assert watches.parse_number(text) == expected


@pytest.mark.no_db
def test_the_four_comparisons():
    assert watches.satisfies("<", 1, 2) and not watches.satisfies("<", 2, 2)
    assert watches.satisfies("<=", 2, 2) and not watches.satisfies("<=", 3, 2)
    assert watches.satisfies(">", 3, 2) and not watches.satisfies(">", 2, 2)
    assert watches.satisfies(">=", 2, 2) and not watches.satisfies(">=", 1, 2)


@pytest.fixture
async def client():
    app = create_app(run_background=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8710") as c:
        yield c


async def _watched(fake, shop, column="price", op="<=", threshold=180):  # noqa: F811
    first, a = await _watch(fake, shop)
    rule = await watches.add(a["id"], "s1", column, op, threshold)
    return a, rule


async def test_a_price_that_crosses_the_line_is_an_alert_once(fake, shop, client):  # noqa: F811
    a, rule = await _watched(fake, shop)  # price <= 180: Kingston costs 189,00, G.Skill 179,00, Patriot 169,00 already
    shop.edit("listing1.html", "189,00 zł", "175,00 zł")  # Kingston drops below the line
    _, s1 = await _run(a)
    stored = await scrape_store.get(s1["id"])
    assert [(x["label"], x["value"], x["was"]) for x in stored["alerts"]] == [("Kingston Fury 16GB DDR4 3200", "175,00 zł", "189,00 zł")]
    assert stored["alerts"][0]["rule_id"] == str(rule["id"]) and stored["alerts"][0]["op"] == "<="
    assert s1["result"].startswith("Watch alerts (1)") and "! Kingston Fury 16GB DDR4 3200: price 175,00 zł (<= 180, was 189,00 zł)" in s1["result"]
    _, again = await _run(a)  # nothing moved: the cheap rows are still cheap, and that is not news
    assert (await scrape_store.get(again["id"]))["alerts"] is None
    assert not again["result"].startswith("Watch alerts")
    shop.edit("listing1.html", "175,00 zł", "199,00 zł")  # back above the line...
    await _run(a)
    shop.edit("listing1.html", "199,00 zł", "170,00 zł")  # ...and below again: news again
    _, third = await _run(a)
    assert [x["value"] for x in (await scrape_store.get(third["id"]))["alerts"]] == ["170,00 zł"]


async def test_alerts_show_under_updates_even_without_other_changes(fake, shop, client):  # noqa: F811
    a, _ = await _watched(fake, shop)
    shop.edit("listing1.html", "189,00 zł", "175,00 zł")
    _, s1 = await _run(a)
    (update,) = (await client.get("/api/inbox")).json()["updates"]
    assert update["id"] == str(s1["id"]) and update["alerts"][0]["value"] == "175,00 zł"
    assert update["counts"]["changed"] == 1  # the price change is reported as a change too
    await client.post(f"/api/subtasks/{s1['id']}/seen", headers=PANEL)
    assert (await client.get("/api/inbox")).json()["updates"] == []


async def test_a_new_row_below_the_line_alerts_and_has_no_previous_value(fake, shop, client):  # noqa: F811
    first, a = await _watch(fake, shop)
    await watches.add(a["id"], "s1", "price", "<", 100)
    shop.edit("listing3.html", "</ul>", '<li class="product"><a class="name" href="p/bargain.html">Bargain Kit</a> <span class="price">59,00 zł</span> <span class="rating" data-score="4.0">4.0/5</span></li></ul>')
    _, s1 = await _run(a)
    stored = await scrape_store.get(s1["id"])
    assert [x["label"] for x in stored["alerts"]] == ["Bargain Kit"] and stored["alerts"][0]["was"] is None


async def test_the_first_run_after_adding_a_watch_reports_rows_already_below_the_line(fake, shop, client):  # noqa: F811
    first, a = await _watch(fake, shop)
    await watches.add(a["id"], "s1", "price", "<=", 170)  # only Patriot (169,00) is below
    stored_first = await scrape_store.get(first["id"])  # the plain task the automation came from has no alerts
    assert stored_first["alerts"] is None
    _, s1 = await _run(a)
    # nothing changed since the saved-from task, which already had Patriot below the line: still not news
    assert (await scrape_store.get(s1["id"]))["alerts"] is None


async def test_greater_than_rules_work_too(fake, shop, client):  # noqa: F811
    first, a = await _watch(fake, shop)
    await watches.add(a["id"], "s1", "rating", ">=", 4.9)  # nobody has it yet
    shop.edit("listing1.html", 'data-score="4.2">4.2/5', 'data-score="4.9">4.9/5')  # Patriot's rating rises
    _, s1 = await _run(a)
    assert [x["label"] for x in (await scrape_store.get(s1["id"]))["alerts"]] == ["Patriot Viper 16GB DDR4 3200"]


async def test_other_steps_and_plain_tasks_never_alert(fake, shop):  # noqa: F811
    first, a = await _watch(fake, shop)
    await watches.add(a["id"], "s1", "price", "<=", 1000)  # everything matches
    stored = await scrape_store.get(first["id"])
    assert stored["alerts"] is None  # the plain task has no automation, so no rules apply
    p = await db.pool()
    steps = a["steps"]
    await p.execute("update automations set steps = $2 where id = $1", a["id"], [{**steps[0], "key": "s9"}])
    run = await automations.start_run(a["id"])
    assert (await subtasks(run["id"]))["s9"]["kind"] == "scrape"  # a step with another key: the rule is for "s1"
    assert await watches.alerts_for({"task_id": run["id"], "key": "s9"}, ["price"], [{"price": "1"}], None) == []


async def test_rules_are_checked_against_the_automations_real_steps_and_columns(fake, shop, client):  # noqa: F811
    first, a = await _watch(fake, shop)
    with pytest.raises(watches.WatchError, match="no column “colour”.*name, url, price, rating"):
        await watches.add(a["id"], "s1", "colour", "<", 5)
    with pytest.raises(watches.WatchError, match="scrape step"):
        await watches.add(a["id"], "nope", "price", "<", 5)
    for bad in (("price", "!=", 5), ("price", "<", float("inf")), ("price", "<", True), ("", "<", 5), ("x" * 61, "<", 5)):
        with pytest.raises(watches.WatchError):
            await watches.add(a["id"], "s1", *bad)
    again = await watches.add(a["id"], "s1", "price", "<", 5)
    assert (await watches.add(a["id"], "s1", "price", "<", 5))["id"] == again["id"]  # the same rule is one rule
    info = await watches.overview(a["id"])
    assert [(s["key"], s["columns"]) for s in info["steps"]] == [("s1", ["name", "url", "price", "rating"])]
    assert [(r["column_name"], r["op"], r["threshold"]) for r in info["rules"]] == [("price", "<", 5.0)]


async def test_the_number_of_watches_is_capped(fake, shop):  # noqa: F811
    first, a = await _watch(fake, shop)
    for i in range(watches.MAX_RULES_PER_AUTOMATION):
        await watches.add(a["id"], "s1", "price", "<", i)
    with pytest.raises(watches.WatchError, match="At most"):
        await watches.add(a["id"], "s1", "price", "<", 999)


async def test_the_api_lists_adds_and_removes_watches(fake, shop, client):  # noqa: F811
    first, a = await _watch(fake, shop)
    base = f"/api/automations/{a['id']}/watches"
    body = {"step_key": "s1", "column": "price", "op": "<=", "threshold": 150}
    assert (await client.post(base, json=body)).status_code == 403  # CSRF guard
    r = await client.post(base, json=body, headers=PANEL)
    assert r.status_code == 201 and r.json()["threshold"] == 150
    info = (await client.get(base)).json()
    assert info["steps"][0]["columns"] == ["name", "url", "price", "rating"] and len(info["rules"]) == 1
    assert (await client.post(base, json={**body, "column": "nope"}, headers=PANEL)).status_code == 400
    assert (await client.post(base, json={**body, "op": "=="}, headers=PANEL)).status_code == 422
    assert (await client.get("/api/automations/00000000-0000-0000-0000-000000000000/watches")).status_code == 404
    rule_id = info["rules"][0]["id"]
    assert (await client.delete(f"/api/watches/{rule_id}")).status_code == 403
    assert (await client.delete(f"/api/watches/{rule_id}", headers=PANEL)).status_code == 200
    assert (await client.delete(f"/api/watches/{rule_id}", headers=PANEL)).status_code == 404
    assert (await client.get(base)).json()["rules"] == []


async def test_deleting_the_automation_removes_its_watches(fake, shop):  # noqa: F811
    first, a = await _watch(fake, shop)
    await watches.add(a["id"], "s1", "price", "<", 5)
    p = await db.pool()
    await p.execute("delete from automations where id = $1", a["id"])
    assert await p.fetchval("select count(*) from watch_rules") == 0


async def test_without_a_unique_column_a_matching_row_is_new_only_if_that_whole_row_did_not_match_before(fake, shop):  # noqa: F811
    first, a = await _watch(fake, shop)
    p = await db.pool()
    await p.execute("insert into watch_rules (automation_id, step_key, column_name, op, threshold) values ($1, 's1', 'temp', '<=', 10)", a["id"])
    run = await automations.start_run(a["id"])
    names = ["city", "temp"]
    before = [{"city": "A", "temp": "12"}, {"city": "A", "temp": "9"}, {"city": "B", "temp": "9"}]  # no column is unique
    after = before + [{"city": "A", "temp": "8"}]
    found = await watches.alerts_for({"task_id": run["id"], "key": "s1"}, names, after, before)
    assert [x["value"] for x in found] == ["8"] and found[0]["was"] is None  # the row that was already cold is not news


async def test_an_alert_alone_is_news_even_when_nothing_else_changed(fake, shop, client):  # noqa: F811
    first, a = await _watch(fake, shop)
    _, s1 = await _run(a)  # nothing changed in the shop
    assert await scrape_store.updates() == []
    stored = await scrape_store.get(s1["id"])
    alert = {"rule_id": "x", "column": "price", "op": "<=", "threshold": 180.0, "label": "Kit", "value": "99,00 zł", "was": None}
    await scrape_store.save(s1["id"], names=stored["fields"], items=stored["items"], recipe=stored["recipe"], pages=1,
                            reused=True, more_pages=False, changes=None, alerts=[alert])
    (update,) = await scrape_store.updates()
    assert str(update["id"]) == str(s1["id"]) and update["counts"] is None and update["alerts"][0]["label"] == "Kit"
    assert (await client.post("/api/inbox/updates/seen", headers=PANEL)).json()["marked"] >= 1  # "mark all" covers alerts too
    assert await scrape_store.updates() == []
