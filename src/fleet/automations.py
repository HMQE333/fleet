"""Saved automations: replay the steps of a task that worked, by hand or on a schedule.

A run is an ordinary task whose subtasks are copied from the saved steps, so it goes
through the same queue, leases, approvals and audit log. It skips the planner (the
operator already saw and approved these exact steps) and gets one review round, which
only writes the answer — no new AI decisions about which accounts to touch.
"""

import logging
from datetime import UTC, datetime
from uuid import UUID

from fleet import costs, db, events, models, schedules

log = logging.getLogger("fleet.automations")

STEP_FIELDS = ("key", "title", "kind", "instructions", "start_url", "depends_on", "model_tier")
BROWSER_KINDS = ("browser", "browser_fast")


class AutomationError(ValueError):
    pass


async def create_from_task(task_id: UUID, name: str, schedule: dict | None, timezone: str) -> dict:
    schedule = schedules.validate(schedule)
    schedules.zone(timezone)
    p = await db.pool()
    task = await p.fetchrow("select * from tasks where id = $1", task_id)
    if task is None:
        raise AutomationError("No such task")
    if task["status"] != "done":
        raise AutomationError("Only a task that finished successfully can be saved as an automation")
    done = await p.fetch(
        "select * from subtasks where task_id = $1 and status = 'done' order by round, created_at", task_id
    )
    if not done:
        raise AutomationError("This task has no successful steps to save")
    keys = {s["key"] for s in done}
    recipes = {r["subtask_id"]: r["recipe"] for r in await p.fetch(
        "select subtask_id, recipe from scrape_results where subtask_id = any($1)", [s["id"] for s in done])}
    steps = [
        {
            **{f: s[f] for f in STEP_FIELDS},
            "depends_on": [d for d in s["depends_on"] if d in keys],
            "profile_id": str(s["requested_profile_id"]) if s["requested_profile_id"] else None,
            # What the browser actually did, so replays can follow it with the fast browser.
            "trace": s["trace"] if s["kind"] in BROWSER_KINDS and s["trace"] else None,
            # What a scrape step learned, so scheduled runs read the pages for free until the site changes.
            "recipe": recipes.get(s["id"]),
        }
        for s in done
    ]
    settings = {k: task[k] for k in ("max_parallel", "require_approval", "allowed_domains", "allow_deep_research", "use_trust_rules")}
    settings["budget_usd"] = float(task["budget_usd"])
    row = await p.fetchrow(
        """
        insert into automations (name, prompt, steps, settings, schedule, timezone, next_run_at, source_task_id)
        values ($1, $2, $3, $4, $5, $6, $7, $8) returning *
        """,
        name.strip() or task["prompt"][:60], task["prompt"], steps, settings, schedule, timezone,
        schedules.next_run(schedule, timezone, datetime.now(UTC)), task_id,
    )
    return dict(row)


async def update(automation_id: UUID, **changes) -> dict:
    p = await db.pool()
    current = await p.fetchrow("select * from automations where id = $1", automation_id)
    if current is None:
        raise AutomationError("No such automation")
    name = changes.get("name", current["name"])
    enabled = changes.get("enabled", current["enabled"])
    timezone = changes.get("timezone", current["timezone"])
    schedules.zone(timezone)
    schedule = schedules.validate(changes["schedule"]) if "schedule" in changes else current["schedule"]
    settings = dict(current["settings"])
    for key in ("require_approval", "budget_usd"):
        if changes.get(key) is not None:
            settings[key] = changes[key]
    next_run_at = schedules.next_run(schedule, timezone, datetime.now(UTC)) if enabled else None
    row = await p.fetchrow(
        """
        update automations set name = $2, enabled = $3, timezone = $4, schedule = $5, settings = $6, next_run_at = $7
        where id = $1 returning *
        """,
        automation_id, name, enabled, timezone, schedule, settings, next_run_at,
    )
    return dict(row)


async def update_trace(automation_id: UUID, key: str, trace: list[dict]) -> bool:
    """A replay found that the page had changed and healed its path (R3): the browser step with this key keeps the path
    that worked today, so the next run follows it without a model call. Only that step's trace changes. False when there
    is no such browser step, or the saved path is already this one."""
    p = await db.pool()
    async with p.acquire() as conn, conn.transaction():
        auto = await conn.fetchrow("select steps from automations where id = $1 for update", automation_id)
        if auto is None:
            return False
        steps = [dict(s) for s in auto["steps"]]
        step = next((s for s in steps if s.get("key") == key and s.get("kind") in BROWSER_KINDS), None)
        if step is None or step.get("trace") == list(trace):
            return False
        step["trace"] = list(trace)
        await conn.execute("update automations set steps = $2 where id = $1", automation_id, steps)
    return True


async def _create_run(conn, auto, trigger: str) -> dict | None:
    """Insert the task + subtasks for one run inside the caller's transaction. None = previous run still going."""
    if auto["last_task_id"]:
        busy = await conn.fetchval(
            "select status not in ('done', 'failed', 'cancelled') from tasks where id = $1", auto["last_task_id"]
        )
        if busy:
            return None
    s = auto["settings"]
    task = await conn.fetchrow(
        """
        insert into tasks (prompt, status, plan_summary, round, max_rounds, max_parallel, require_approval,
            allowed_domains, budget_usd, plan_review, automation_id, allow_deep_research, estimated_cost, use_trust_rules)
        values ($1, 'running', $2, 1, 1, $3, $4, $5, $6, 'never', $7, $8, $9, $10) returning *
        """,
        auto["prompt"], f"Saved automation “{auto['name']}” ({trigger} run): replaying {len(auto['steps'])} approved steps.",
        s.get("max_parallel", 4), s.get("require_approval", True), s.get("allowed_domains", []),
        s.get("budget_usd", 2.0), auto["id"], s.get("allow_deep_research", False),
        sum(costs.expected(st["kind"], st.get("model_tier", "fast")) for st in auto["steps"]), s.get("use_trust_rules", True),
    )
    existing_profiles = {r["id"] for r in await conn.fetch("select id from profiles")}
    fast = await models.fast_browser_enabled()
    for step in auto["steps"]:
        profile_id = UUID(step["profile_id"]) if step.get("profile_id") else None
        status, error = "pending", None
        kind, guide = step["kind"], None
        if kind in BROWSER_KINDS and step.get("trace") and step.get("start_url") and fast:
            # Learned once (by whichever engine), replayed fast: follow the recorded path; Jev only
            # decides where the page changed, and the full agent still takes over if it gets stuck.
            kind, guide = "browser_fast", step["trace"]
        if kind == "scrape":
            guide = step.get("recipe")
        if profile_id and profile_id not in existing_profiles:
            profile_id, status, error = None, "failed", "The profile this step used no longer exists. Save the automation again from a new run."
        await conn.execute(
            """
            insert into subtasks (task_id, round, key, title, kind, instructions, start_url, depends_on, profile_id,
                requested_profile_id, model_tier, status, error, finished_at, guide, expected_cost)
            values ($1, 1, $2, $3, $4, $5, $6, $7, $8, $8, $9, $10, $11, case when $10 = 'failed' then now() end, $12, $13)
            """,
            task["id"], step["key"], step["title"], kind, step["instructions"], step.get("start_url"),
            step.get("depends_on") or [], profile_id, step.get("model_tier", "fast"), status, error, guide,
            costs.expected(kind, step.get("model_tier", "fast")),
        )
    await conn.execute("update automations set last_run_at = now(), last_task_id = $2 where id = $1", auto["id"], task["id"])
    return dict(task)


async def _announce(auto, task: dict | None, trigger: str) -> None:
    """Events go out after commit (they reference the new task row)."""
    if task is None:
        await events.emit(
            "automation.skipped", f"“{auto['name']}”: previous run still going, skipped this {trigger} run",
            task_id=auto["last_task_id"], level="warn",
        )
    else:
        await events.emit("task.status", f"Started from automation “{auto['name']}” ({trigger})", task_id=task["id"],
                          data={"status": "running", "automation_id": str(auto["id"])})


async def start_run(automation_id: UUID, trigger: str = "manual") -> dict | None:
    """Start one run now. Returns None when the previous run is still going (runs never overlap)."""
    p = await db.pool()
    async with p.acquire() as conn, conn.transaction():
        auto = await conn.fetchrow("select * from automations where id = $1 for update", automation_id)
        if auto is None:
            raise AutomationError("No such automation")
        task = await _create_run(conn, auto, trigger)
    await _announce(auto, task, trigger)
    return task


async def run_due() -> int:
    """Start every automation whose time has come — once per slot, even with several conductors."""
    p = await db.pool()
    started = 0
    while True:
        async with p.acquire() as conn, conn.transaction():
            auto = await conn.fetchrow(
                "select * from automations where enabled and next_run_at <= now() "
                "order by next_run_at for update skip locked limit 1"
            )
            if auto is None:
                return started
            # Advance first: a slot is used whether the run starts or is skipped. Missed slots
            # (e.g. the computer was off) are not replayed: one run now, then the next future slot.
            await conn.execute(
                "update automations set next_run_at = $2 where id = $1",
                auto["id"], schedules.next_run(auto["schedule"], auto["timezone"], datetime.now(UTC)),
            )
            task = await _create_run(conn, auto, "scheduled")
        await _announce(auto, task, "scheduled")
        started += task is not None
