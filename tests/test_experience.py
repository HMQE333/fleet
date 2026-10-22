"""The experience base (fleet/experience.py, RULES.md §7): every browser run leaves a record, and recording never breaks a step."""

import pytest

from fleet import db, experience, llm, vault
from fleet.executors import RunContext


@pytest.mark.no_db
@pytest.mark.parametrize("url, domain", [
    ("https://Shop.Test/search?q=1", "shop.test"), ("http://127.0.0.1:8080/x", "127.0.0.1"), ("https://example.com.", "example.com"),
    (None, ""), ("", ""), ("not a url", ""),
])
def test_domain_of(url, domain):
    assert experience.domain_of(url) == domain


async def _ctx(**subtask):
    p = await db.pool()
    task = dict(await p.fetchrow("insert into tasks (prompt) values ('find a pi') returning *"))
    st = dict(await p.fetchrow(
        "insert into subtasks (task_id, round, key, title, kind, instructions, start_url) values ($1, 1, 'b1', 'B', $2, $3, $4) returning *",
        task["id"], subtask.get("kind", "browser_fast"), subtask.get("instructions", "search for a pi"),
        subtask.get("start_url", "https://shop.test/")))
    ctx = RunContext(task=task, subtask=st, dependency_results={}, worker_id="w", usage=llm.Usage())
    ctx.usage.cost_usd = subtask.get("cost", 0.0123)
    return ctx


async def test_a_run_is_written_down_with_its_path_outcome_cost_and_time():
    ctx = await _ctx()
    path = [{"kind": "fill", "label": "Search products", "value": "rpi 5"}, {"kind": "click", "label": "Search", "value": None}]
    await experience.record(ctx, "done", "Jev confirmed the goal is met", path, 3.4)
    p = await db.pool()
    row = dict(await p.fetchrow("select * from experience"))
    assert row["domain"] == "shop.test" and row["goal"] == "search for a pi" and row["kind"] == "browser_fast"
    assert row["status"] == "done" and row["reason"] == "Jev confirmed the goal is met" and row["actions"] == path
    assert float(row["cost_usd"]) == 0.0123 and abs(row["seconds"] - 3.4) < 0.01
    assert row["task_id"] == ctx.task["id"] and row["subtask_id"] == ctx.subtask["id"]


async def test_a_reason_is_redacted_and_capped_and_an_empty_path_is_fine():
    vault.remember("sk-or-verysecretkey123", "OPENROUTER_API_KEY")
    ctx = await _ctx(kind="browser")
    await experience.record(ctx, "error", "OpenRouter said no to sk-or-verysecretkey123 " + "x" * 1000, None, -1)
    p = await db.pool()
    row = dict(await p.fetchrow("select * from experience"))
    assert "verysecret" not in row["reason"] and "[OPENROUTER_API_KEY]" in row["reason"]
    assert len(row["reason"]) <= experience.REASON_CHARS and row["actions"] == [] and row["seconds"] == 0


async def test_recording_never_fails_the_step(monkeypatch):
    ctx = await _ctx()

    async def broken():
        raise RuntimeError("database away")

    monkeypatch.setattr(db, "pool", broken)
    await experience.record(ctx, "done", "", [], 1)  # no exception


async def test_both_workers_record_after_the_run():
    import inspect

    from fleet.executors import browser, browser_fast

    for mod in (browser, browser_fast):
        assert "experience.record(" in inspect.getsource(mod), mod.__name__


async def test_two_runs_in_the_same_clock_tick_still_have_a_latest_one():
    """The clock steps in 15.6 ms on Windows, so two rows can share `created_at` to the microsecond; `seq` is what
    makes "the latest" one answer. Both questions have to read the row that was written last."""
    p = await db.pool()
    same_tick = await p.fetchval("select now()")
    path = [{"kind": "click", "label": "Second", "value": None}]
    for goal, status, reason, actions in (
        ("open the archive", "done", "", [{"kind": "click", "label": "First", "value": None}]),
        ("open the archive", "done", "", path),
        ("open the archive", "stopped", "This page asks for a login", []),
        ("open the archive", "rejected", "The operator didn't approve “Post”", []),
    ):
        await p.execute(
            "insert into experience (domain, goal, kind, status, reason, actions, created_at) "
            "values ('tick.test', $1, 'browser_fast', $2, $3, $4, $5)",
            goal, status, reason, actions, same_tick)
    assert len({r["created_at"] for r in await p.fetch("select created_at from experience")}) == 1
    assert await experience.best_path("tick.test", "open the archive") == path
    assert "approve" in (await experience.site_notes("tick.test")).lower()
