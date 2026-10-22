"""The cost report (READINESS.md, gate C4): what Fleet spent in a period, per role, and what each saved automation's
replays cost against its first run.

Fleet records money on two levels: every step carries its own `subtasks.cost_usd` (the model calls that step made) and
the task carries `tasks.cost_usd`, the whole bill including planning and review rounds. So the spend per role is the
sum of step costs grouped by the step's kind (browser, browser_fast, fetch, research, video, scrape, crawl), plus a
"planner" row for the part of the tasks' bill that no step accounts for (planning, review, summaries).

An automation's first run is its source task (the one the operator saved it from); every later task with that
`automation_id` is a replay. `saved_usd` is what those replays would have cost at first-run prices minus what they
cost in fact: replays × first_run_usd − replay_total_usd. It can be negative when replays got more expensive than the
first run (a drifted path that fell back to the model every time), which is exactly when the operator wants to know.

Only aggregates leave this module: no prompts, no page text, nothing from the vault.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fleet import db

MAX_DAYS = 365
PLANNER = "planner"


def _usd(value) -> float:
    return round(float(value or 0), 4)


async def summary(days: int = 30) -> dict:
    """Spend in the last `days` days (tasks created in the period): per role, in total, and per automation."""
    days = max(1, min(int(days), MAX_DAYS))
    since = datetime.now(UTC) - timedelta(days=days)
    p = await db.pool()

    per_kind = await p.fetch(
        """
        select s.kind, count(*) as steps, coalesce(sum(s.cost_usd), 0) as usd
        from subtasks s join tasks t on t.id = s.task_id
        where t.created_at >= $1
        group by s.kind
        """,
        since,
    )
    totals = await p.fetchrow(
        "select count(*) as tasks, coalesce(sum(cost_usd), 0) as usd from tasks where created_at >= $1", since)
    steps_usd = sum(_usd(r["usd"]) for r in per_kind)
    total_usd = _usd(totals["usd"])
    by_role = [{"role": r["kind"], "usd": _usd(r["usd"]), "steps": int(r["steps"])} for r in per_kind]
    # Whatever the tasks' bill holds beyond their steps went to planning, review and summaries.
    by_role.append({"role": PLANNER, "usd": _usd(max(total_usd - steps_usd, 0.0)), "steps": int(totals["tasks"])})
    by_role.sort(key=lambda r: (-r["usd"], r["role"]))

    autos = await p.fetch(
        """
        select a.id, a.name, a.source_task_id,
               coalesce(src.cost_usd, 0) as first_run_usd,
               (select count(*) from tasks r where r.automation_id = a.id and r.created_at >= $1) as replays,
               (select coalesce(sum(r.cost_usd), 0) from tasks r where r.automation_id = a.id and r.created_at >= $1)
                   as replay_total_usd
        from automations a left join tasks src on src.id = a.source_task_id
        where src.created_at >= $1
           or exists (select 1 from tasks r where r.automation_id = a.id and r.created_at >= $1)
        order by a.created_at desc
        """,
        since,
    )
    automations = []
    for a in autos:
        first, replays, replay_total = _usd(a["first_run_usd"]), int(a["replays"]), _usd(a["replay_total_usd"])
        automations.append({
            "automation_id": str(a["id"]),
            "name": a["name"],
            "source_task_id": str(a["source_task_id"]) if a["source_task_id"] else None,
            "first_run_usd": first,
            "replays": replays,
            "replay_total_usd": replay_total,
            "saved_usd": round(replays * first - replay_total, 4),
        })
    return {"days": days, "since": since.isoformat(), "total_usd": total_usd, "tasks": int(totals["tasks"]),
            "by_role": by_role, "automations": automations}
