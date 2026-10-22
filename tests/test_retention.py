"""Retention: old finished tasks lose their screenshots, histories and step log lines; nothing else.

The rules under test: off by default; only finished tasks older than the period; only the three heavy things; the audit trail, answers,
scraped data and running work are never touched; only files named after step ids inside the data folders; throttled and logged.
"""

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from fleet import db, limits, retention
from fleet.api import create_app
from fleet.config import settings

PANEL = {"X-Fleet-Client": "panel"}


@pytest.fixture
async def data(tmp_path, monkeypatch):
    """Data folders in a temp dir, and a retention clock that has never run."""
    monkeypatch.setattr(settings(), "data_dir", tmp_path)
    settings().screenshots_dir.mkdir(parents=True, exist_ok=True)
    settings().histories_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(retention, "_next_check", 0.0)
    return tmp_path


async def _task(status: str, days_ago: float | None, *, shots: int = 2) -> dict:
    """A task with one step, screenshots and a history file on disk, and step / log / audit events."""
    p = await db.pool()
    t = await p.fetchval(
        "insert into tasks (prompt, status, finished_at) values ('x', $1, case when $2::float8 is null then null else now() - make_interval(days => 0, secs => $2::float8 * 86400) end) returning id",
        status, None if days_ago is None else float(days_ago))
    s = await p.fetchval(
        "insert into subtasks (task_id, round, key, title, kind, instructions, status, result) values ($1, 1, 's1', 'step', 'browser_fast', 'x', 'done', 'the answer') returning id", t)
    shots_dir = settings().screenshots_dir / str(s)
    shots_dir.mkdir()
    for i in range(shots):
        (shots_dir / f"{i:03d}.jpg").write_bytes(b"x" * 1000)
    history = settings().histories_dir / f"{s}.json"
    history.write_text("{}", encoding="utf-8")
    for typ in ("step", "step", "log", "human.request", "human.response", "task.status", "plan"):
        await p.execute("insert into events (task_id, subtask_id, type, message) values ($1, $2, $3, $3)", t, s, typ)
    return {"task": t, "step": s, "shots": shots_dir, "history": history}


async def _events(task_id) -> list[str]:
    p = await db.pool()
    return sorted(r["type"] for r in await p.fetch("select type from events where task_id = $1", task_id))


async def test_old_finished_tasks_lose_their_screenshots_histories_and_step_lines_and_nothing_else(data):
    old = await _task("done", 40)
    p = await db.pool()
    await p.execute("insert into scrape_results (subtask_id, fields, items) values ($1, '[\"a\"]', '[{\"a\": \"1\"}]')", old["step"])
    result = await retention.prune(30)
    assert result == {"tasks": 1, "files": 3, "bytes": 2002, "events": 3}
    assert not old["shots"].exists() and not old["history"].exists()
    assert await _events(old["task"]) == ["human.request", "human.response", "plan", "task.status"]  # the audit trail stays
    assert await p.fetchval("select result from subtasks where id = $1", old["step"]) == "the answer"
    assert await p.fetchval("select count(*) from scrape_results where subtask_id = $1", old["step"]) == 1  # scraped data stays
    assert await p.fetchval("select count(*) from tasks where id = $1", old["task"]) == 1


async def test_recent_unfinished_and_stopped_work_is_never_touched(data):
    recent = await _task("done", 5)
    running = await _task("running", None)
    waiting = await _task("awaiting_approval", None)
    ancient_running = await _task("running", 400)  # an old date on a task that never finished means nothing
    result = await retention.prune(30)
    assert result["tasks"] == 0
    for t in (recent, running, waiting, ancient_running):
        assert t["shots"].exists() and t["history"].exists() and len(await _events(t["task"])) == 7
    failed, cancelled = await _task("failed", 90), await _task("cancelled", 90)
    assert (await retention.prune(30))["tasks"] == 2  # a failed or stopped task is finished too
    assert not failed["shots"].exists() and not cancelled["history"].exists()


async def test_only_files_named_after_steps_inside_the_data_folders_are_removed(data):
    old = await _task("done", 40)
    stranger = settings().screenshots_dir / "not-a-step-id"
    stranger.mkdir()
    (stranger / "keep.jpg").write_bytes(b"1")
    outside = data / "notes.txt"
    outside.write_text("keep", encoding="utf-8")
    other_history = settings().histories_dir / "someone-elses.json"
    other_history.write_text("{}", encoding="utf-8")
    await retention.prune(30)
    assert not old["shots"].exists()
    assert (stranger / "keep.jpg").exists() and outside.exists() and other_history.exists()


async def test_nothing_happens_when_retention_is_off_and_pruning_twice_is_harmless(data):
    old = await _task("done", 400)
    assert await retention.prune(0) == {"tasks": 0, "files": 0, "bytes": 0, "events": 0}
    assert await retention.run_if_due() is None  # the default is "keep everything"
    assert old["shots"].exists() and len(await _events(old["task"])) == 7
    first = await retention.prune(30)
    again = await retention.prune(30)
    assert first["events"] == 3 and again["files"] == 0 and again["events"] == 0


async def test_the_automatic_cleanup_runs_when_due_then_waits_and_leaves_a_note(data, monkeypatch):
    old = await _task("done", 40)
    await limits.put({"retention_days": 30})
    result = await retention.run_if_due()
    assert result and result["files"] == 3 and not old["shots"].exists()
    p = await db.pool()
    note = await p.fetchval("select message from events where type = 'retention.pruned'")
    assert "3 screenshot/history files" in note and "3 step log lines" in note
    retention._next_check = 0.0  # look again straight away: it is not time yet (six hours between clean-ups)
    newer = await _task("done", 45)
    assert await retention.run_if_due() is None and newer["shots"].exists()
    await p.execute("update settings set value = $1 where key = 'retention'", {"last_run": (datetime.now(UTC) - timedelta(hours=7)).isoformat()})
    retention._next_check = 0.0
    assert (await retention.run_if_due())["tasks"] >= 1 and not newer["shots"].exists()


async def test_looking_for_work_is_throttled(data, monkeypatch):
    await limits.put({"retention_days": 30})
    looks = []
    real = limits.effective

    async def counted():
        looks.append(1)
        return await real()

    monkeypatch.setattr(limits, "effective", counted)
    assert await retention.run_if_due() is not None  # the first look
    old = await _task("done", 40)
    assert await retention.run_if_due() is None  # the next one is minutes away, whatever happened meanwhile
    assert len(looks) == 1 and old["shots"].exists()  # and it did not even read the settings: the loop runs every second


async def test_the_conductors_loop_asks_for_the_cleanup(data, monkeypatch):
    from fleet import conductor

    calls = []

    async def spy():
        calls.append(1)

    monkeypatch.setattr(retention, "run_if_due", spy)
    await conductor.Conductor().tick()
    assert calls == [1]


@pytest.fixture
async def client():
    app = create_app(run_background=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8710") as c:
        yield c


async def test_the_api_shows_storage_and_cleans_up_on_request(data, client):
    old = await _task("done", 40)
    usage = (await client.get("/api/maintenance/usage")).json()
    assert usage["screenshots_bytes"] == 2000 and usage["step_events"] == 3 and usage["retention_days"] == 0
    assert (await client.post("/api/maintenance/prune")).status_code == 403  # CSRF guard
    r = await client.post("/api/maintenance/prune", headers=PANEL)
    assert r.status_code == 400 and "Set how many days" in r.json()["detail"] and old["shots"].exists()
    await limits.put({"retention_days": 30})
    r = await client.post("/api/maintenance/prune", headers=PANEL)
    assert r.status_code == 200 and r.json()["files"] == 3 and not old["shots"].exists()
    assert (await client.get("/api/maintenance/usage")).json()["screenshots_bytes"] == 0


async def test_retention_is_one_of_the_limits_with_a_hard_ceiling(client):
    rows = {r["name"]: r for r in (await client.get("/api/settings/limits")).json()}
    assert rows["retention_days"]["default"] == 0 and rows["retention_days"]["max"] == 3650
    assert (await client.put("/api/settings/limits", json={"values": {"retention_days": 4000}}, headers=PANEL)).status_code == 400
    assert (await client.put("/api/settings/limits", json={"values": {"retention_days": 90}}, headers=PANEL)).status_code == 200
