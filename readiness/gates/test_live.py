"""L1/L2 gates: real models, real money, within FLEET_READINESS_BUDGET_USD (default $0.25) per run.

Skipped (reported "unavailable") without a key or with --no-live. Every call is written to readiness/spend.jsonl.
"""

import statistics
import time

import pytest

from fleet import llm, models
from fleet.fastbrowser import decide, engine
from fleet.fastbrowser.cdp import CDP

# C5 is the only gate here that needs Postgres (it runs the whole of Fleet, task row and all); the rest drive the
# browser and the models directly, so they are marked no_db one by one.
GOAL = "Search the shop for 'raspberry pi 5' and open the first result."


async def _observe(ws, url):
    from fleet.fastbrowser.page import Page

    async with CDP(ws) as cdp:
        page = await Page.open(cdp, url)
        try:
            return await page.observe(screenshot=False)
        finally:
            await page.close()


@pytest.mark.no_db
async def test_b3_jev_decision_is_cheap_and_fast(live, browser_ws, site):
    state = await _observe(browser_ws, f"{site}/shop.html")
    model = models.ROLES["jev"]["default"]  # no database in these tests: the default role model
    costs, latencies = [], []
    for _ in range(3):
        usage = llm.Usage()
        started = time.perf_counter()
        decision = await decide.choose(state, GOAL, [], model=model, usage=usage)
        latencies.append(time.perf_counter() - started)
        costs.append(usage.cost_usd)
        live.record("B3", "jev decision", usage.cost_usd, choice=decision["choice"], latency_s=round(latencies[-1], 2))
    assert statistics.median(costs) <= 0.002, costs
    assert statistics.median(latencies) <= 4.0, latencies


@pytest.mark.no_db
async def test_b4_field_value_is_cheap(live, browser_ws, site):
    state = await _observe(browser_ws, f"{site}/shop.html")
    action = next(a for a in state["actions"] if a["kind"] == "fill")
    usage = llm.Usage()
    text = await decide.field_text(GOAL, action, state, [], model=models.ROLES["fast"]["default"], usage=usage)
    live.record("B4", "field value", usage.cost_usd, text=text)
    assert text and "raspberry" in text.lower()
    assert usage.cost_usd <= 0.002, usage.cost_usd


@pytest.mark.no_db
async def test_r4_replay_costs_a_tenth(live, browser_ws, site):
    jev, fast = models.ROLES["jev"]["default"], models.ROLES["fast"]["default"]
    first = llm.Usage()
    async with CDP(browser_ws) as cdp:
        out = await engine.run(cdp, url=f"{site}/shop.html", goal=GOAL, hooks=engine.Hooks(), usage=first, decide_model=jev,
                               text_model=fast, allow_private=True, max_steps=8)
    live.record("R4", "first run", first.cost_usd, status=out.status, steps=len(out.steps))
    assert out.finished, out.reason
    assert out.trace, "the first run recorded no path"
    second = llm.Usage()
    async with CDP(browser_ws) as cdp:
        again = await engine.run(cdp, url=f"{site}/shop.html", goal=GOAL, hooks=engine.Hooks(), usage=second, decide_model=jev,
                                 text_model=fast, guide=out.trace, allow_private=True, max_steps=8)
    live.record("R4", "replay", second.cost_usd, status=again.status, guided=sum(s["guided"] for s in again.steps))
    assert again.finished, again.reason
    assert second.cost_usd <= 0.1 * max(first.cost_usd, 1e-6), f"replay ${second.cost_usd:.4f} vs first ${first.cost_usd:.4f}"


@pytest.mark.no_db
async def test_a7_live_benchmark(live, browser_ws):
    """Real sites, real Jev, the local headless browser. Needs A1 (a browser profile of Fleet's own) to be meaningful;
    until then it drives the test browser directly."""
    jev, fast = models.ROLES["jev"]["default"], models.ROLES["fast"]["default"]
    tasks = [  # (start, goal, what the final page must show: "text:..." in its text or "url:..." in its address)
        ("https://www.wikipedia.org", "Search for Gdańsk, open the article and find its population.", "text:Gdańsk"),
        ("https://books.toscrape.com/", "Open the Mystery category and then the first book on it.", "text:Mystery"),
        ("https://quotes.toscrape.com/", "Go to the next page of quotes.", "url:/page/2"),
    ]
    passed = 0
    for url, goal, expect in tasks:
        usage = llm.Usage()
        async with CDP(browser_ws) as cdp:
            out = await engine.run(cdp, url=url, goal=goal, hooks=engine.Hooks(), usage=usage, decide_model=jev, text_model=fast,
                                   max_steps=12, max_seconds=120)
        final = out.final or {}
        kind, _, want = expect.partition(":")
        shown = (final.get("full_text") or final.get("text") or "") if kind == "text" else (final.get("url") or "")
        ok = out.finished and want.lower() in shown.lower()
        live.record("A7", goal, usage.cost_usd, status=out.status, ok=ok)
        passed += ok
    assert passed / len(tasks) >= 0.8, f"{passed}/{len(tasks)} live tasks passed"


@pytest.mark.no_db
async def test_k5_second_visit_is_cheaper(live, browser_ws, site):
    """A paraphrased goal on a site Fleet already handled: the path the base would supply (K2/K3 prove the lookup; here the
    first visit's own actions stand in for it) makes the second visit nearly free even though the words changed."""
    jev, fast = models.ROLES["jev"]["default"], models.ROLES["fast"]["default"]
    first = llm.Usage()
    async with CDP(browser_ws) as cdp:
        out = await engine.run(cdp, url=f"{site}/shop.html", goal=GOAL, hooks=engine.Hooks(), usage=first, decide_model=jev,
                               text_model=fast, allow_private=True, max_steps=8)
    live.record("K5", "first visit", first.cost_usd, status=out.status)
    assert out.finished and out.trace, out.reason
    known = out.trace
    second = llm.Usage()
    async with CDP(browser_ws) as cdp:
        again = await engine.run(cdp, url=f"{site}/shop.html", goal="Look up 'raspberry pi 5' in the shop and open the top hit.",
                                 hooks=engine.Hooks(), usage=second, decide_model=jev, text_model=fast, guide=known,
                                 allow_private=True, max_steps=8)
    live.record("K5", "second visit (paraphrased goal)", second.cost_usd, status=again.status)
    assert again.finished, again.reason
    assert second.cost_usd <= 0.3 * max(first.cost_usd, 1e-6), f"second ${second.cost_usd:.4f} vs first ${first.cost_usd:.4f}"


async def test_c5_a_simple_task_costs_cents(live, fake, monkeypatch):
    """The whole of Fleet, for real: planning (no model round at all for a request that names one page and one action),
    a browser step on a local Chrome profile (real Jev), and the answer. The task must cost cents, and its saved replay
    must cost almost nothing — a saved run of one finished step needs no answer round either.
    `fake` is only used to undo its planner stand-ins: this test wants the real planner."""
    from fleet import automations, db, planner, profiles, worker
    from fleet.executors import browser_fast
    from tests.conftest import finished, running_fleet, wait_for

    monkeypatch.undo()  # the real planner and the real executors
    monkeypatch.setitem(worker.EXECUTORS, "browser_fast", browser_fast.run)
    assert planner.plan.__module__ == "fleet.planner"
    await profiles.add_local("Readiness scratch", headless=True)
    p = await db.pool()
    task = dict(await p.fetchrow(
        "insert into tasks (prompt, plan_review, budget_usd) values ($1, 'never', 0.25) returning *",
        "Open https://books.toscrape.com/ and open the Mystery category. Tell me the title of the first book listed there."))
    async with running_fleet():
        t = await wait_for(lambda: finished(task["id"]), timeout=240)
    live.record("C5", "first run", float(t["cost_usd"]), status=t["status"])
    assert t["status"] == "done", t["error"]
    assert float(t["cost_usd"]) <= 0.03, f"first run cost ${float(t['cost_usd']):.4f}"
    subs = [dict(r) for r in await p.fetch("select kind, status from subtasks where task_id = $1", task["id"])]
    assert [s["kind"] for s in subs] == ["browser_fast"], subs  # one step, no research detour

    a = await automations.create_from_task(task["id"], "First Mystery book", None, "UTC")
    run = await automations.start_run(a["id"])
    async with running_fleet():
        r = await wait_for(lambda: finished(run["id"]), timeout=240)
    live.record("C5", "replay", float(r["cost_usd"]), status=r["status"])
    assert r["status"] == "done", r["error"]
    assert float(r["cost_usd"]) <= 0.005, f"replay cost ${float(r['cost_usd']):.4f}"
