"""Updates: a saved automation's scrape that found changes shows in the Inbox (as news, not a question) until the
operator marks it as seen; runs without changes stay quiet.
"""

from fleet import scrape_store

from .conftest import subtasks
from .test_changes import _run, _watch, client, shop  # noqa: F401  (the test shop and its helpers are shared)

PANEL = {"X-Fleet-Client": "panel"}


async def _updates(api) -> list[dict]:
    r = await api.get("/api/inbox")
    assert r.status_code == 200
    return r.json()["updates"]


async def test_a_run_that_found_changes_shows_up_and_a_quiet_run_does_not(fake, shop, client):  # noqa: F811
    _, a = await _watch(fake, shop)
    assert await _updates(client) == []  # the first run (a plain task) is not news
    shop.edit("listing1.html", "189,00 zł", "175,00 zł")
    _, s1 = await _run(a)
    (update,) = await _updates(client)
    assert update["id"] == str(s1["id"]) and update["automation_name"] == "RAM prices"
    assert update["counts"] == {"added": 0, "removed": 0, "changed": 1, "unchanged": 11}
    assert update["partial"] is False and update["rows"] == 12 and update["title"] == s1["title"]
    quiet_run, quiet = await _run(a)  # nothing moved since
    assert [u["id"] for u in await _updates(client)] == [str(s1["id"])]
    assert (await scrape_store.get(quiet["id"]))["changes"]["counts"]["changed"] == 0


async def test_marking_an_update_as_seen_removes_only_that_one(fake, shop, client):  # noqa: F811
    _, a = await _watch(fake, shop)
    shop.edit("listing1.html", "189,00 zł", "175,00 zł")
    _, first = await _run(a)
    shop.edit("listing1.html", "175,00 zł", "170,00 zł")
    _, second = await _run(a)
    assert [u["id"] for u in await _updates(client)] == [str(second["id"]), str(first["id"])]  # newest first
    assert (await client.post(f"/api/subtasks/{second['id']}/seen")).status_code == 403  # CSRF guard
    assert (await client.post(f"/api/subtasks/{second['id']}/seen", headers=PANEL)).status_code == 200
    assert [u["id"] for u in await _updates(client)] == [str(first["id"])]
    assert (await client.post(f"/api/subtasks/{second['id']}/seen", headers=PANEL)).status_code == 404  # already seen
    assert (await client.post("/api/subtasks/00000000-0000-0000-0000-000000000000/seen", headers=PANEL)).status_code == 404


async def test_mark_all_as_seen(fake, shop, client):  # noqa: F811
    _, a = await _watch(fake, shop)
    shop.edit("listing1.html", "189,00 zł", "175,00 zł")
    await _run(a)
    shop.edit("listing1.html", "175,00 zł", "170,00 zł")
    await _run(a)
    assert len(await _updates(client)) == 2
    assert (await client.post("/api/inbox/updates/seen")).status_code == 403
    r = await client.post("/api/inbox/updates/seen", headers=PANEL)
    assert r.status_code == 200 and r.json()["marked"] >= 2 and await _updates(client) == []
    shop.edit("listing1.html", "170,00 zł", "165,00 zł")
    _, third = await _run(a)  # new changes after clearing show up again
    assert [u["id"] for u in await _updates(client)] == [str(third["id"])]


async def test_incomparable_and_partial_results(fake, shop, client, monkeypatch):  # noqa: F811
    from fleet.config import settings

    _, a = await _watch(fake, shop)
    shop.edit("listing1.html", "189,00 zł", "175,00 zł")
    monkeypatch.setattr(settings(), "scrape_max_pages", 1)  # this run only reads page 1
    _, s1 = await _run(a)
    (update,) = await _updates(client)
    assert update["partial"] is True and update["counts"]["changed"] == 1  # edits are still real news
    await client.post("/api/inbox/updates/seen", headers=PANEL)
    stored = await scrape_store.get(s1["id"])
    changes = {**stored["changes"], "comparable": False, "reason": "columns differ"}
    await scrape_store.save(s1["id"], names=stored["fields"], items=stored["items"], recipe=stored["recipe"], pages=1,
                            reused=True, more_pages=False, changes=changes)
    assert await _updates(client) == []  # a table that couldn't be compared says nothing about changes


async def test_saving_a_result_again_makes_its_news_unseen_again(fake, shop, client):  # noqa: F811
    _, a = await _watch(fake, shop)
    shop.edit("listing1.html", "189,00 zł", "175,00 zł")
    _, s1 = await _run(a)
    await client.post(f"/api/subtasks/{s1['id']}/seen", headers=PANEL)
    assert await _updates(client) == []
    stored = await scrape_store.get(s1["id"])  # e.g. the step was retried and found the same changes again
    await scrape_store.save(s1["id"], names=stored["fields"], items=stored["items"], recipe=stored["recipe"], pages=1,
                            reused=True, more_pages=False, changes=stored["changes"])
    assert len(await _updates(client)) == 1


async def test_a_plain_task_never_shows_as_an_update(fake, shop, client):  # noqa: F811
    first, _ = await _watch(fake, shop)
    assert (await scrape_store.get(first["id"]))["changes"] is None
    assert await _updates(client) == []
    assert all(s["status"] == "done" for s in (await subtasks(first["task_id"])).values())
    stored = await scrape_store.get(first["id"])  # even if changes were written to it, it isn't an automation run
    counts = {"added": 1, "removed": 0, "changed": 0, "unchanged": 3}
    await scrape_store.save(first["id"], names=stored["fields"], items=stored["items"], recipe=stored["recipe"], pages=1,
                            reused=False, more_pages=False, changes={"comparable": True, "partial": False, "counts": counts})
    assert await _updates(client) == []
