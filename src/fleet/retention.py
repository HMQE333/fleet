"""Retention: the heavy, least useful traces of old work are cleaned up, nothing else.

What grows without bound is screenshots (a file per browser step), saved agent histories, and the step-by-step activity of every
task. After `retention_days` (Settings → Limits; 0 = keep everything, the default) the finished tasks older than that lose exactly
those three things. Never touched: running or unfinished tasks, the tasks, steps and answers themselves, scraped data and its
history (change monitoring and watches depend on it), saved automations, and the audit trail (approvals, Inbox answers, trust rules,
registrations, password reveals: every event that is not a plain step or log line).
"""

import logging
import shutil
import time
from datetime import UTC, datetime, timedelta

from fleet import db, events, limits
from fleet.config import settings

log = logging.getLogger("fleet.retention")

HEAVY_EVENTS = ("step", "log")  # the only event types ever pruned
INTERVAL = timedelta(hours=6)  # between automatic clean-ups
CHECK_EVERY = 600.0  # seconds between looks at whether one is due
_next_check = 0.0


def _size(path) -> int:
    if path.is_file():
        return path.stat().st_size
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file()) if path.is_dir() else 0


async def usage() -> dict:
    s = settings()
    p = await db.pool()
    return {"screenshots_bytes": _size(s.screenshots_dir), "histories_bytes": _size(s.histories_dir),
            "step_events": await p.fetchval("select count(*) from events where type = any($1)", list(HEAVY_EVENTS)),
            "retention_days": (await limits.effective()).retention_days}


async def prune(days: int) -> dict:
    """Clean up finished tasks that ended more than `days` days ago. `days` of 0 or less does nothing."""
    result = {"tasks": 0, "files": 0, "bytes": 0, "events": 0}
    if days <= 0:
        return result
    s, p = settings(), await db.pool()
    rows = await p.fetch(
        """
        select s.id as subtask_id, t.id as task_id from tasks t join subtasks s on s.task_id = t.id
        where t.status in ('done', 'failed', 'cancelled') and t.finished_at is not null
          and t.finished_at < now() - make_interval(days => $1)
        """, days)
    tasks = {r["task_id"] for r in rows}
    for r in rows:
        name = str(r["subtask_id"])  # a UUID from the database: never a path from outside
        for target in (s.screenshots_dir / name, s.histories_dir / f"{name}.json"):
            if target.exists() and target.resolve().parent in (s.screenshots_dir.resolve(), s.histories_dir.resolve()):
                result["bytes"] += _size(target)
                result["files"] += sum(1 for f in target.rglob("*") if f.is_file()) if target.is_dir() else 1
                shutil.rmtree(target) if target.is_dir() else target.unlink()
    if tasks:
        done = await p.execute("delete from events where task_id = any($1) and type = any($2)", list(tasks), list(HEAVY_EVENTS))
        result["events"] = int(done.rsplit(" ", 1)[1])
    result["tasks"] = len(tasks)
    return result


async def run_if_due() -> dict | None:
    """Called from the conductor's loop: looks every few minutes, cleans up at most every six hours, and only when the operator
    has set a retention period."""
    global _next_check
    if time.monotonic() < _next_check:
        return None
    _next_check = time.monotonic() + CHECK_EVERY
    days = (await limits.effective()).retention_days
    if days <= 0:
        return None
    p = await db.pool()
    last = await p.fetchval("select value from settings where key = 'retention'") or {}
    if last.get("last_run") and datetime.now(UTC) - datetime.fromisoformat(last["last_run"]) < INTERVAL:
        return None
    result = await prune(days)
    await p.execute(
        "insert into settings (key, value) values ('retention', $1) on conflict (key) do update set value = excluded.value, updated_at = now()",
        {"last_run": datetime.now(UTC).isoformat(), **result})
    if result["files"] or result["events"]:
        await events.emit("retention.pruned", f"Cleaned up after {days} days: {result['files']} screenshot/history files "
                          f"({result['bytes'] // 1024} KB) and {result['events']} step log lines of {result['tasks']} finished tasks")
    return result
