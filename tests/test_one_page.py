"""A task that names one page: no planner round, and a saved run of it needs no answer round either
(fleet/planner.py, fleet/conductor.py, READINESS.md gate C5)."""

import pytest

from fleet import conductor, llm, planner

PROFILES = [{"name": "Local", "notes": "", "allowed_domains": [], "general_use": True}]
PROMPT = "Open https://quotes.toscrape.com/ and go to the next page of quotes. Tell me the author of the first quote."


def task(prompt: str, **extra) -> dict:
    return {"prompt": prompt, "allowed_domains": [], "budget_usd": 0.25, "cost_usd": 0, "require_approval": True, **extra}


@pytest.mark.no_db
def test_one_named_page_becomes_one_fast_browser_step():
    plan = planner.one_page(task(PROMPT), PROFILES)
    assert plan is not None, "a request naming one page should need no planner round"
    assert plan.decision == "plan" and len(plan.subtasks) == 1
    step = plan.subtasks[0]
    assert step.kind == "browser_fast"
    assert step.start_url == "https://quotes.toscrape.com/"
    assert step.instructions == PROMPT  # the request itself is the goal; nothing is paraphrased by a model
    assert step.model_tier == "fast" and step.depends_on == [] and step.profile is None
    assert "quotes.toscrape.com" in plan.summary


@pytest.mark.no_db
async def test_the_shortcut_costs_nothing_and_asks_no_model(monkeypatch):
    async def no_calls(**kw):
        raise AssertionError("the planner model was called for a one-page request")

    monkeypatch.setattr(llm, "structured", no_calls)
    usage = llm.Usage()
    plan = await planner.plan(task(PROMPT), PROFILES, usage)
    assert plan.subtasks[0].kind == "browser_fast"
    assert usage.cost_usd == 0 and usage.input_tokens == 0


@pytest.mark.no_db
@pytest.mark.parametrize("prompt", [
    "Open https://a.test/ and https://b.test/ and tell me which is cheaper",   # two pages
    "Find the cheapest raspberry pi and tell me where",                        # no page at all
    "Research https://a.test/ and tell me what the company does",              # another source
    "Open https://a.test/ and scrape every product into a table",              # a table step
    "Compare the price on https://a.test/ with the usual one",                 # more than this page
    "Watch https://a.test/ for changes",                                       # a watch, not a task
    f"Open https://a.test/ and {'click the next link and then read it ' * 12}",  # too much for one step
])
def test_anything_past_that_page_still_gets_the_planner(prompt):
    assert planner.one_page(task(prompt), PROFILES) is None


@pytest.mark.no_db
def test_the_words_inside_the_url_do_not_count():
    """"quotes.toscrape.com" is not a scrape, and a research.test host is not research."""
    assert planner.one_page(task("Open https://research.test/x and read the title"), PROFILES) is not None


@pytest.mark.no_db
def test_no_shortcut_without_a_profile_or_outside_the_allowed_domains():
    assert planner.one_page(task(PROMPT), []) is None  # no browser profile: the planner must say so
    assert planner.one_page(task(PROMPT, allowed_domains=["shop.test"]), PROFILES) is None
    assert planner.one_page(task(PROMPT, allowed_domains=["toscrape.com"]), PROFILES) is not None  # a subdomain is in


@pytest.mark.no_db
def test_a_saved_run_of_one_finished_step_answers_with_that_step():
    one = [{"kind": "browser_fast", "status": "done", "result": "Marilyn Monroe", "title": "Open quotes"}]
    automation = {"automation_id": "a1"}
    assert conductor.Conductor._saved_run_answer(automation, one) == "Marilyn Monroe"
    assert conductor.Conductor._saved_run_answer({"automation_id": None}, one) is None  # a first run is reviewed
    assert conductor.Conductor._saved_run_answer(automation, []) is None
    for bad in ({"status": "failed"}, {"result": None}):
        assert conductor.Conductor._saved_run_answer(automation, [{**one[0], **bad}]) is None


@pytest.mark.no_db
def test_a_saved_run_of_several_steps_is_reviewed_unless_every_step_is_a_report():
    automation = {"automation_id": "a1"}
    tables = [{"kind": "scrape", "status": "done", "result": "| a |", "title": "Products"},
              {"kind": "crawl", "status": "done", "result": "| b |", "title": "Pages"}]
    assert conductor.Conductor._saved_run_answer(automation, tables) == "### Products\n| a |\n\n### Pages\n| b |"
    mixed = [tables[0], {"kind": "browser_fast", "status": "done", "result": "clicked", "title": "Step"}]
    assert conductor.Conductor._saved_run_answer(automation, mixed) is None


async def test_a_finished_tasks_cost_is_final(fake, monkeypatch):
    """The review round is charged before the task says done, so whoever reads a finished task (the panel, an
    automation, gate C5) sees what it really cost and not the cost without its last round."""
    from fleet import db, planner
    from tests.conftest import finished, new_task, running_fleet, sub, subtasks, wait_for

    fake.plan.subtasks = [sub("s1", kind="fetch", url="https://a.test/")]
    real_review = planner.review

    async def paid_review(task, subs, profiles, usage, last_round):
        usage.input_tokens, usage.output_tokens, usage.cost_usd = 1000, 100, 0.0123
        return await real_review(task, subs, profiles, usage, last_round)

    monkeypatch.setattr(planner, "review", paid_review)
    task = await new_task()
    async with running_fleet():
        row = await wait_for(lambda: finished(task["id"]))
    assert row["status"] == "done"
    assert float(row["cost_usd"]) == pytest.approx(0.0123)  # the row that first said "done" already knew
    p = await db.pool()
    assert float(await p.fetchval("select cost_usd from tasks where id = $1", task["id"])) == pytest.approx(0.0123)
    assert (await subtasks(task["id"]))["s1"]["status"] == "done"


@pytest.mark.no_db
def test_a_profiles_user_data_dir_is_absolute(tmp_path, monkeypatch):
    """Chrome exits at once (code 21) on a relative --user-data-dir, and Fleet's data dir is relative by default."""
    from fleet import browsers

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("FLEET_DATA_DIR", "data")
    assert browsers._data_dir({"id": "p1", "data_dir": "data/profiles/p1"}).is_absolute()
    assert browsers._data_dir({"id": "p1"}).is_absolute()
