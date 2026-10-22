"""Conductor: drives each task through plan → dispatch → review → (more rounds) → done.

State lives in Postgres, so the conductor can restart (or run in several
processes) without losing work: every step claims its row with a lease.
"""

import asyncio
import logging
import os
import socket
import uuid

from fleet import automations, costs, db, events, llm, models, planner, profiles, retention
from fleet.config import settings

log = logging.getLogger("fleet.conductor")

ACTIVE = ("pending", "queued", "running", "needs_human")
BROWSER_KINDS = ("browser", "browser_fast")

NOT_RETRIED = (
    "Stopped after it had already done something irreversible (an approved action), so Fleet won't "
    "retry it automatically: that could do it twice. Check the account, then retry by hand if needed."
)


def needs_plan_review(task: dict, planned: list, estimate: float = 0.0) -> bool:
    """The operator okays plans that act on their accounts or would cost real money, unless they chose otherwise."""
    if task["plan_review"] == "always":
        return True
    if task["plan_review"] == "never":
        return False
    return any(ps.kind in BROWSER_KINDS for ps in planned) or estimate >= costs.REVIEW_PLAN_FROM_USD


class Conductor:
    def __init__(self):
        self.id = f"conductor-{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}"
        self._jobs: set[asyncio.Task] = set()

    def _spawn(self, coro) -> None:
        t = asyncio.create_task(coro)
        self._jobs.add(t)
        t.add_done_callback(self._jobs.discard)

    async def run_forever(self, stop: asyncio.Event) -> None:
        log.info("conductor %s up", self.id)
        while not stop.is_set():
            try:
                await self.tick()
            except Exception:
                log.exception("conductor tick failed")
            try:
                await asyncio.wait_for(stop.wait(), timeout=1.0)
            except TimeoutError:
                pass
        for t in list(self._jobs):
            t.cancel()

    async def tick(self) -> None:
        await self._reap()
        await automations.run_due()
        await retention.run_if_due()
        while task := await self._claim("queued", "planning"):
            self._spawn(self._plan(task))
        await self._enforce_budgets()
        await self._fail_unrunnable()
        await self._promote()
        await self._skip_stuck()
        while task := await self._claim_reviewable():
            self._spawn(self._review(task))

    # -- claiming ---------------------------------------------------------

    async def _claim(self, from_status: str, to_status: str) -> dict | None:
        p = await db.pool()
        return db.row(
            await p.fetchrow(
                """
                update tasks set status = $2, lease_owner = $3, lease_expires_at = now() + interval '15 minutes'
                where id = (select id from tasks where status = $1 order by created_at for update skip locked limit 1)
                returning *
                """,
                from_status,
                to_status,
                self.id,
            )
        )

    async def _claim_reviewable(self) -> dict | None:
        p = await db.pool()
        return db.row(
            await p.fetchrow(
                """
                update tasks set status = 'reviewing', lease_owner = $1, lease_expires_at = now() + interval '15 minutes'
                where id = (
                    select t.id from tasks t
                    where t.status = 'running'
                      and not exists (select 1 from subtasks s where s.task_id = t.id and s.status = any($2))
                    order by t.updated_at
                    for update skip locked
                    limit 1
                )
                returning *
                """,
                self.id,
                list(ACTIVE),
            )
        )

    # -- housekeeping -----------------------------------------------------

    async def _reap(self) -> None:
        p = await db.pool()
        await profiles.reap_expired()
        stale = await p.fetch(
            f"""
            update subtasks set
                status = case when side_effects then 'failed' when attempts < max_attempts then 'queued' else 'failed' end,
                error = case when side_effects then $1 else 'Worker stopped responding' end,
                worker_id = null
            where status in ('running', 'needs_human') and heartbeat_at < now() - interval '{settings().lease_seconds * 2} seconds'
            returning task_id, id, title, status
            """,
            NOT_RETRIED,
        )
        for r in stale:
            await events.emit(
                "subtask.status", f"{r['title']}: worker stopped responding — {r['status']}",
                task_id=r["task_id"], subtask_id=r["id"], level="warn", data={"status": r["status"]},
            )
        await p.execute(
            """
            update tasks set status = case status when 'planning' then 'queued' else 'running' end, lease_owner = null
            where status in ('planning', 'reviewing') and lease_expires_at < now()
            """
        )

    async def _promote(self) -> None:
        """pending → queued when every dependency is done; → skipped when one failed."""
        p = await db.pool()
        rs = await p.fetch(
            """
            with deps as (
                select s.id, s.task_id, s.title,
                    bool_and(d.status = 'done') filter (where d.id is not null) as all_done,
                    bool_or(d.status in ('failed', 'skipped', 'cancelled')) as any_dead,
                    count(d.id) as n_found, cardinality(s.depends_on) as n_wanted
                from subtasks s
                join tasks t on t.id = s.task_id and t.status = 'running'
                left join lateral (
                    select distinct on (key) id, status from subtasks
                    where task_id = s.task_id and key = any(s.depends_on)
                    order by key, round desc
                ) d on true
                where s.status = 'pending'
                group by s.id
            )
            update subtasks s set status = case when deps.any_dead then 'skipped' else 'queued' end,
                error = case when deps.any_dead then 'A subtask it depends on did not succeed' else null end,
                finished_at = case when deps.any_dead then now() else null end
            from deps
            where s.id = deps.id and (deps.any_dead or deps.n_wanted = 0 or (deps.all_done and deps.n_found >= deps.n_wanted))
            returning s.task_id, s.id, s.title, s.status
            """
        )
        for r in rs:
            if r["status"] == "skipped":
                await events.emit(
                    "subtask.status", f"{r['title']}: skipped — a dependency did not succeed",
                    task_id=r["task_id"], subtask_id=r["id"], level="warn", data={"status": "skipped"},
                )

    async def _fail_unrunnable(self) -> None:
        """Browser subtasks that can never get a profile: theirs is off or broken, or none are enabled."""
        p = await db.pool()
        rs = await p.fetch(
            """
            update subtasks s set status = 'failed', finished_at = now(),
                error = case when s.requested_profile_id is null
                        then 'No profile is marked for general browsing. Mark one on the Profiles page, or name an account in the prompt'
                        else 'Its profile is switched off or in an error state. Fix it on the Profiles page' end
            from tasks t
            where t.id = s.task_id and t.status = 'running' and s.kind in ('browser', 'browser_fast') and s.status = 'queued'
              and (
                (s.requested_profile_id is null
                 and not exists (select 1 from profiles where enabled and general_use and status <> 'error'))
                or exists (select 1 from profiles pr where pr.id = s.requested_profile_id and (not pr.enabled or pr.status = 'error'))
              )
            returning s.task_id, s.id, s.title, s.error
            """
        )
        for r in rs:
            await events.emit(
                "subtask.status", f"{r['title']}: failed — {r['error']}",
                task_id=r["task_id"], subtask_id=r["id"], level="warn", data={"status": "failed"},
            )

    async def _enforce_budgets(self) -> None:
        p = await db.pool()
        # Spent money only grows, so a step that doesn't fit now never will (the review keeps its reserve).
        rs = await p.fetch(
            f"""
            update subtasks s set status = 'skipped', finished_at = now(),
                error = format('Skipped to stay within the task budget: it would need about $%s, and $%s is left '
                               '(after keeping $%s for the final answer). Raise the budget and use Run again.',
                               round(s.expected_cost, 2), round(greatest(t.budget_usd - t.cost_usd, 0), 2),
                               {costs.REVIEW_RESERVE})
            from tasks t
            where t.id = s.task_id and t.status = 'running' and s.status in ('pending', 'queued')
              and t.cost_usd + s.expected_cost + {costs.REVIEW_RESERVE} > t.budget_usd
            returning s.task_id, s.id, s.title, s.error
            """
        )
        for r in rs:
            await events.emit(
                "subtask.status", f"{r['title']}: {r['error']}",
                task_id=r["task_id"], subtask_id=r["id"], level="warn", data={"status": "skipped"},
            )

    async def _skip_stuck(self) -> None:
        """A round where only pending subtasks remain has a dependency cycle or a missing key."""
        p = await db.pool()
        rs = await p.fetch(
            """
            update subtasks s set status = 'skipped', error = 'Its dependencies can never finish', finished_at = now()
            from tasks t
            where t.id = s.task_id and t.status = 'running' and s.status = 'pending'
              and s.created_at < now() - interval '5 seconds'
              and not exists (
                  select 1 from subtasks o where o.task_id = s.task_id
                    and (o.status in ('queued', 'running', 'needs_human') or o.finished_at > now() - interval '5 seconds')
              )
            returning s.task_id, s.id, s.title
            """
        )
        for r in rs:
            await events.emit(
                "subtask.status", f"{r['title']}: skipped — its dependencies can never finish",
                task_id=r["task_id"], subtask_id=r["id"], level="warn", data={"status": "skipped"},
            )

    # -- planning and review ---------------------------------------------

    async def _enabled_profiles(self) -> list[dict]:
        p = await db.pool()
        return db.rows(await p.fetch("select * from profiles where enabled order by name"))

    async def _insert_subtasks(self, task: dict, round_: int, planned: list[planner.PlannedSubtask],
                               profs: list[dict]) -> tuple[int, float]:
        """Insert a round's subtasks. Returns (how many, their expected cost)."""
        by_name = {pr["name"].lower(): pr for pr in profs}
        fast_enabled = await models.fast_browser_enabled()
        estimate = 0.0
        p = await db.pool()
        existing = {r["key"] for r in await p.fetch("select key from subtasks where task_id = $1", task["id"])}
        keys = {ps.key for ps in planned} | existing
        n = 0
        for ps in planned:
            if ps.key in existing:
                continue
            profile_id, status, error = None, "pending", None
            kind, tier = ps.kind, ps.model_tier
            if kind == "browser_fast" and (not fast_enabled or not ps.start_url):
                kind = "browser"  # fast browser switched off, or nowhere to start: use the full agent
            if kind == "research" and tier == "smart" and not task["allow_deep_research"]:
                tier = "fast"  # deep research costs ~$1.50 a step: only with the operator's explicit permission
                await events.emit(
                    "plan.warning", f"“{ps.title}”: deep research is off for this task, so it uses normal research. "
                    "Turn on “Allow deep research” in the task options if you need it.", task_id=task["id"], level="warn",
                )
            expected = costs.expected(kind, tier)
            estimate += expected
            if kind in BROWSER_KINDS:
                if not profs:
                    status, error = "failed", "No browser profiles are enabled in Fleet"
                elif ps.profile:
                    match = by_name.get(ps.profile.lower())
                    if match:
                        profile_id = match["id"]
                    else:
                        await events.emit(
                            "plan.warning",
                            f"Planner asked for unknown profile “{ps.profile}” — a general-browsing profile will be used",
                            task_id=task["id"], level="warn",
                        )
            if ps.kind in ("fetch", "video", "scrape", "crawl") and not ps.start_url:
                status, error = "failed", f"{ps.kind.capitalize()} subtask without a URL"
            await p.execute(
                """
                insert into subtasks (task_id, round, key, title, kind, instructions, start_url, depends_on, profile_id,
                    requested_profile_id, model_tier, status, error, finished_at, expected_cost)
                values ($1, $2, $3, $4, $5, $6, $7, $8, $9, $9, $10, $11, $12, case when $11 = 'failed' then now() end, $13)
                """,
                task["id"], round_, ps.key, ps.title, kind, ps.instructions, ps.start_url,
                [d for d in ps.depends_on if d in keys and d != ps.key], profile_id, tier, status, error, expected,
            )
            n += 1
        await p.execute("update tasks set estimated_cost = estimated_cost + $2 where id = $1", task["id"], estimate)
        return n, estimate

    async def _charge(self, task_id, usage: llm.Usage) -> None:
        """What a round cost, written to the task — and emptied out of `usage`, so charging early (before the task is
        marked done, so a finished task's cost is final) and again in the `finally` never charges twice."""
        if not (usage.input_tokens or usage.output_tokens or usage.cost_usd):
            return
        p = await db.pool()
        await p.execute(
            "update tasks set input_tokens = input_tokens + $2, output_tokens = output_tokens + $3, cost_usd = cost_usd + $4 where id = $1",
            task_id, usage.input_tokens, usage.output_tokens, usage.cost_usd,
        )
        usage.input_tokens, usage.output_tokens, usage.cost_usd = 0, 0, 0.0

    async def _set_task(self, task_id, status: str, **fields) -> None:
        p = await db.pool()
        cols = ", ".join(f"{k} = ${i}" for i, k in enumerate(fields, start=3))
        finished = ", finished_at = now()" if status in ("done", "failed", "cancelled") else ""
        await p.execute(
            f"update tasks set status = $2, lease_owner = null{', ' + cols if cols else ''}{finished} "
            "where id = $1 and status <> 'cancelled'",
            task_id, status, *fields.values(),
        )

    async def _plan(self, task: dict) -> None:
        usage = llm.Usage()
        await events.emit("task.status", "Planning…", task_id=task["id"], data={"status": "planning"})
        try:
            profs = await self._enabled_profiles()
            plan = await planner.plan(task, profs, usage)
            await self._charge(task["id"], usage)  # before any status change, for the same reason as in _review
            if plan.decision == "refuse":
                await self._set_task(task["id"], "failed", error=plan.reason)
                await events.emit("task.status", f"Refused: {plan.reason}", task_id=task["id"], level="warn", data={"status": "failed"})
                return
            if not plan.subtasks:
                await self._set_task(task["id"], "failed", error="The planner produced no subtasks")
                return
            n, estimate = await self._insert_subtasks(task, 1, plan.subtasks, profs)
            status = "awaiting_approval" if needs_plan_review(task, plan.subtasks, estimate) else "running"
            await self._set_task(task["id"], status, plan_summary=plan.summary, round=1)
            await events.emit(
                "plan", f"{plan.summary} (estimated cost about ${estimate + costs.REVIEW_RESERVE:.2f})", task_id=task["id"],
                data={"status": status, "subtasks": [ps.model_dump() for ps in plan.subtasks], "count": n,
                      "estimate": round(estimate, 2)},
            )
            if status == "awaiting_approval":
                await events.emit("task.status", "Plan ready: waiting for your OK before any worker starts",
                                  task_id=task["id"], level="warn", data={"status": status})
        except Exception as e:
            log.exception("planning failed for %s", task["id"])
            await self._set_task(task["id"], "failed", error=f"Planning failed: {e}")
            await events.emit("task.status", f"Planning failed: {e}", task_id=task["id"], level="error", data={"status": "failed"})
        finally:
            await self._charge(task["id"], usage)

    @staticmethod
    def _saved_run_answer(task: dict, subtasks: list[dict]) -> str | None:
        """The answer a saved run needs no model to write, or None when the planner must review it: a single finished
        step speaks for itself (the operator already approved what it does), and scrape / crawl steps are their own
        reports. Only for an automation's runs — a first run always gets its review round."""
        if not (task["automation_id"] and subtasks) or any(s["status"] != "done" or not s["result"] for s in subtasks):
            return None
        if len(subtasks) == 1:
            return subtasks[0]["result"]
        if all(s["kind"] in TABLE_KINDS for s in subtasks):
            return "\n\n".join(f"### {s['title']}\n{s['result']}" for s in subtasks)
        return None

    async def _review(self, task: dict) -> None:
        usage = llm.Usage()
        p = await db.pool()
        await events.emit("task.status", f"Reviewing round {task['round']}…", task_id=task["id"], data={"status": "reviewing"})
        try:
            subtasks = db.rows(await p.fetch("select * from subtasks where task_id = $1 order by round, created_at", task["id"]))
            if free := self._saved_run_answer(task, subtasks):
                # A saved run whose steps answer for themselves: no model round, so the run costs only its steps.
                await self._set_task(task["id"], "done", result=free)
                await events.emit("task.status", "Done", task_id=task["id"], data={"status": "done"})
                return
            profs = await self._enabled_profiles()
            last_round = task["round"] >= task["max_rounds"] or task["cost_usd"] >= task["budget_usd"]
            rv = await planner.review(task, subtasks, profs, usage, last_round)
            await self._charge(task["id"], usage)  # before the task says done: its cost is final when it finishes
            if rv.decision == "continue" and rv.subtasks and not last_round:
                n, estimate = await self._insert_subtasks(task, task["round"] + 1, rv.subtasks, profs)
                if n:
                    status = "awaiting_approval" if needs_plan_review(task, rv.subtasks, estimate) else "running"
                    await self._set_task(task["id"], status, round=task["round"] + 1)
                    await events.emit(
                        "plan", rv.answer or f"Round {task['round'] + 1}", task_id=task["id"],
                        data={"status": status, "subtasks": [ps.model_dump() for ps in rv.subtasks], "count": n},
                    )
                    if status == "awaiting_approval":
                        await events.emit("task.status", f"Round {task['round'] + 1} planned: waiting for your OK",
                                          task_id=task["id"], level="warn", data={"status": status})
                    return
            await self._set_task(task["id"], "done", result=rv.answer)
            await events.emit("task.status", "Done", task_id=task["id"], data={"status": "done"})
        except Exception as e:
            log.exception("review failed for %s", task["id"])
            done = [s for s in await p.fetch(
                "select title, result from subtasks where task_id = $1 and status = 'done' order by round, created_at",
                task["id"]) if s["result"]]
            found = "\n\n".join(f"### {s['title']}\n{s['result'][:4000]}" for s in done)
            await self._set_task(
                task["id"], "failed", error=f"Review failed: {e}",
                result=(f"The planner couldn't write the final answer ({e}). Here is what the workers found:\n\n{found}"
                        if found else None),
            )
            await events.emit("task.status", f"Review failed: {e}", task_id=task["id"], level="error", data={"status": "failed"})
        finally:
            await self._charge(task["id"], usage)


async def approve_plan(task_id) -> bool:
    p = await db.pool()
    r = await p.fetchrow(
        "update tasks set status = 'running' where id = $1 and status = 'awaiting_approval' returning id", task_id
    )
    if r is None:
        return False
    await events.emit("task.status", "Plan approved by the operator: starting workers", task_id=task_id,
                      data={"status": "running"})
    return True


TABLE_KINDS = ("scrape", "crawl")  # steps whose result is a finished report in itself


class RetryError(ValueError):
    pass


async def retry_subtask(subtask_id) -> dict:
    """Run ONE failed step of a finished task again, in place: the step and the steps that were skipped because of it start over,
    the task goes back to running, and the planner reviews again when they finish. Everything that worked stays as it is.

    Refused when it would be unsafe or meaningless: not a failed step, not of the task's latest round, the task isn't finished
    (still running or cancelled), or the step already did something that can't be undone (running it again could repeat it)."""
    p = await db.pool()
    async with p.acquire() as conn, conn.transaction():
        row = await conn.fetchrow(
            """
            select s.id, s.task_id, s.key, s.title, s.round, s.status, s.side_effects, t.status as task_status, t.round as task_round
            from subtasks s join tasks t on t.id = s.task_id where s.id = $1 for update of s, t
            """, subtask_id)
        if row is None:
            raise RetryError("No such step")
        if row["status"] != "failed":
            raise RetryError("Only a failed step can be retried")
        if row["task_status"] not in ("done", "failed"):
            raise RetryError("The task is still running or was stopped; retry once it has finished")
        if row["round"] != row["task_round"]:
            raise RetryError("This step belongs to an earlier round that the planner already moved past")
        if row["side_effects"]:
            raise RetryError("This step already did something that can't be undone, so it isn't repeated automatically. "
                             "Check the result, then run the task again if you really want it repeated")
        keys = {row["key"]}
        skipped = await conn.fetch(
            "select id, key, depends_on from subtasks where task_id = $1 and round = $2 and status = 'skipped'",
            row["task_id"], row["round"])
        reset, grew = [], True
        while grew:  # the steps that were skipped because of this one, and the ones skipped because of those
            grew = False
            for s in skipped:
                if s["id"] not in reset and keys & set(s["depends_on"]):
                    reset.append(s["id"])
                    keys.add(s["key"])
                    grew = True
        await conn.execute(
            """
            update subtasks set status = 'pending', attempts = 0, result = null, error = null, human_request = null,
                human_response = null, worker_id = null, heartbeat_at = null, started_at = null, finished_at = null,
                run_after = now(), steps = 0, take_over = false
            where id = any($1)
            """, [row["id"], *reset])
        await conn.execute(
            "update tasks set status = 'running', error = null, finished_at = null, lease_owner = null where id = $1", row["task_id"])
    await events.emit(
        "subtask.retry", f"Retrying “{row['title']}” (you asked)" + (f" and {len(reset)} step(s) that were skipped because of it" if reset else ""),
        task_id=row["task_id"], subtask_id=row["id"], data={"also": len(reset)})
    await events.emit("task.status", "Running again: one step is being retried", task_id=row["task_id"], data={"status": "running"})
    return {"retried": str(row["id"]), "also_restarted": len(reset)}


async def cancel_task(task_id) -> bool:
    p = await db.pool()
    async with p.acquire() as conn, conn.transaction():
        r = await conn.fetchrow(
            "update tasks set status = 'cancelled', finished_at = now(), lease_owner = null "
            "where id = $1 and status not in ('done', 'failed', 'cancelled') returning id",
            task_id,
        )
        if r is None:
            return False
        await conn.execute(
            "update subtasks set status = 'cancelled', finished_at = now() where task_id = $1 and status = any($2)",
            task_id, list(ACTIVE),
        )
    await events.emit("task.status", "Cancelled by the operator", task_id=task_id, level="warn", data={"status": "cancelled"})
    return True
