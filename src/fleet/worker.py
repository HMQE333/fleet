"""Worker pool: claims queued subtasks from Postgres and runs them concurrently.

Web slots (fetch/research) need no profile. Browser slots each hold one leased
AdsPower profile, so browser parallelism = min(browser slots, idle profiles).
Several worker processes (or machines) can run side by side: claims use
FOR UPDATE SKIP LOCKED, so no subtask or profile is ever taken twice.
"""

import asyncio
import logging
import os
import socket
import uuid

from fleet import costs, db, events, hitl, profiles
from fleet.config import settings
from fleet.executors import Blocked, ExecResult, RunContext, browser, browser_fast, fetch, research, video
from fleet.executors import crawl as crawl_worker
from fleet.executors import scrape as scrape_worker
from fleet.conductor import NOT_RETRIED
from fleet.llm import LLMError
from fleet.executors.browser import ProfileError

log = logging.getLogger("fleet.worker")

EXECUTORS = {
    "fetch": fetch.run, "research": research.run, "video": video.run, "scrape": scrape_worker.run, "crawl": crawl_worker.run,
    "browser": browser.run, "browser_fast": browser_fast.run,
}

_RETRY_DELAY_PROFILE = 30  # seconds before retrying after AdsPower failed to open a profile

_CLAIM_WEB = f"""
update subtasks s set status = 'running', worker_id = $1, started_at = now(), heartbeat_at = now(),
    attempts = attempts + 1
where s.id = (
    select s2.id from subtasks s2 join tasks t on t.id = s2.task_id
    where s2.status = 'queued' and s2.run_after <= now() and s2.kind in ('fetch', 'research', 'video', 'scrape', 'crawl') and t.status = 'running' and not t.paused
      and t.cost_usd + s2.expected_cost + {costs.REVIEW_RESERVE}
          + (select coalesce(sum(r.expected_cost), 0) from subtasks r
             where r.task_id = s2.task_id and r.status in ('running', 'needs_human')) <= t.budget_usd
      and (select count(*) from subtasks r where r.task_id = s2.task_id and r.status in ('running', 'needs_human')) < t.max_parallel
    order by s2.created_at
    for update of s2 skip locked
    limit 1
)
returning s.*
"""

_BROWSER_CANDIDATES = f"""
select s2.* from subtasks s2 join tasks t on t.id = s2.task_id
where s2.status = 'queued' and s2.run_after <= now() and s2.kind in ('browser', 'browser_fast') and t.status = 'running'
  and not t.paused
  and t.cost_usd + s2.expected_cost + {costs.REVIEW_RESERVE}
        + (select coalesce(sum(r.expected_cost), 0) from subtasks r
           where r.task_id = s2.task_id and r.status in ('running', 'needs_human')) <= t.budget_usd
  and (select count(*) from subtasks r where r.task_id = s2.task_id and r.status in ('running', 'needs_human')) < t.max_parallel
order by s2.created_at
for update of s2 skip locked
limit 10
"""


class Worker:
    def __init__(self, worker_id: str | None = None):
        self.id = worker_id or f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}"
        self.web_active = 0
        self.browser_active = 0
        self._tasks: set[asyncio.Task] = set()

    async def _claim_web(self) -> dict | None:
        p = await db.pool()
        return db.row(await p.fetchrow(_CLAIM_WEB, self.id))

    async def _claim_browser(self) -> tuple[dict, dict] | None:
        p = await db.pool()
        async with p.acquire() as conn, conn.transaction():
            for cand in await conn.fetch(_BROWSER_CANDIDATES):
                prof = await profiles.try_lease(conn, self.id, cand["requested_profile_id"])
                if prof is None:
                    continue
                st = await conn.fetchrow(
                    """
                    update subtasks set status = 'running', worker_id = $2, profile_id = $3, started_at = now(),
                        heartbeat_at = now(), attempts = attempts + 1, take_over = false
                    where id = $1 returning *
                    """,
                    cand["id"],
                    self.id,
                    prof["id"],
                )
                return dict(st), prof
        return None

    def _spawn(self, coro) -> None:
        t = asyncio.create_task(coro)
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    async def run_forever(self, stop: asyncio.Event) -> None:
        s = settings()
        log.info("worker %s up (web slots %d, browser slots %d)", self.id, s.worker_web_slots, s.worker_browser_slots)
        while not stop.is_set():
            claimed = False
            try:
                if self.web_active < s.worker_web_slots and (st := await self._claim_web()):
                    self.web_active += 1
                    self._spawn(self._run(st, None))
                    claimed = True
                if self.browser_active < s.worker_browser_slots and (pair := await self._claim_browser()):
                    self.browser_active += 1
                    self._spawn(self._run(*pair))
                    claimed = True
            except Exception:
                log.exception("claim loop error")
            if not claimed:
                try:
                    await asyncio.wait_for(stop.wait(), timeout=1.0)
                except TimeoutError:
                    pass
        for t in list(self._tasks):
            t.cancel()

    async def _heartbeat(self, st: dict, prof: dict | None) -> None:
        p = await db.pool()
        while True:
            await asyncio.sleep(20)
            await p.execute("update subtasks set heartbeat_at = now() where id = $1", st["id"])
            if prof:
                await profiles.renew(prof["id"], self.id)

    async def _dependency_results(self, st: dict) -> dict[str, str]:
        if not st["depends_on"]:
            return {}
        p = await db.pool()
        rs = await p.fetch(
            "select distinct on (key) key, result from subtasks where task_id = $1 and key = any($2) and status = 'done' "
            "order by key, round desc",
            st["task_id"],
            st["depends_on"],
        )
        return {r["key"]: r["result"] or "" for r in rs}

    async def _run(self, st: dict, prof: dict | None) -> None:
        p = await db.pool()
        task = db.row(await p.fetchrow("select * from tasks where id = $1", st["task_id"]))
        ctx = RunContext(task=task, subtask=st, dependency_results=await self._dependency_results(st), worker_id=self.id, profile=prof)
        where = f" on profile “{prof['name']}”" if prof else ""
        await events.emit(
            "subtask.status", f"{st['title']}: started{where} (attempt {st['attempts']})",
            task_id=st["task_id"], subtask_id=st["id"], data={"status": "running"},
        )
        beat = asyncio.create_task(self._heartbeat(st, prof))
        profile_error = None
        try:
            outcome = await EXECUTORS[st["kind"]](ctx)
            await self._finish(st, "done" if outcome.success else "failed", outcome, None if outcome.success else outcome.result)
        except (Blocked, hitl.HumanTimeout) as e:
            await self._finish(st, "failed", None, str(e))
        except LLMError as e:
            if e.retryable:
                await self._retry_or_fail(st, str(e), delay=15 * st["attempts"])
            else:  # no key, no credits, unknown model: retrying won't help
                await self._finish(st, "failed", None, str(e))
        except hitl.Cancelled:
            pass
        except ProfileError as e:
            profile_error = str(e)
            await self._retry_or_fail(st, f"Profile problem: {e}", delay=_RETRY_DELAY_PROFILE)
        except asyncio.CancelledError:
            await self._retry_or_fail(st, "Worker shut down", delay=0)
            raise
        except Exception as e:
            log.exception("subtask %s crashed", st["id"])
            await self._retry_or_fail(st, f"{type(e).__name__}: {e}", delay=15 * st["attempts"])
        finally:
            beat.cancel()
            await self._charge(st, ctx)
            if prof:
                await profiles.release(prof["id"], self.id, profile_error)
                self.browser_active -= 1
            else:
                self.web_active -= 1

    async def _finish(self, st: dict, status: str, outcome: ExecResult | None, error: str | None) -> None:
        p = await db.pool()
        r = await p.fetchrow(
            """
            update subtasks set status = $2, result = $3, error = $4, steps = greatest(steps, $5), history_path = $6,
                trace = coalesce($7, trace),
                finished_at = now()
            where id = $1 and status in ('running', 'needs_human') returning id
            """,
            st["id"],
            status,
            outcome.result if outcome else None,
            error,
            outcome.steps if outcome else 0,
            outcome.history_path if outcome else None,
            outcome.trace if outcome else None,
        )
        if r is None:
            return  # cancelled meanwhile
        msg = f"{st['title']}: {status}" + (f" — {error[:300]}" if error and status != "done" else "")
        await events.emit(
            "subtask.status", msg, task_id=st["task_id"], subtask_id=st["id"],
            level="info" if status == "done" else "warn", data={"status": status},
        )

    async def _retry_or_fail(self, st: dict, error: str, delay: int) -> None:
        p = await db.pool()
        r = await p.fetchrow(
            f"""
            update subtasks set
                status = case when side_effects then 'failed' when attempts < max_attempts then 'queued' else 'failed' end,
                error = case when side_effects then $3 || ' (' || $2 || ')' else $2 end,
                worker_id = null,
                run_after = now() + interval '{int(delay)} seconds',
                finished_at = case when side_effects or attempts >= max_attempts then now() end
            where id = $1 and status in ('running', 'needs_human') returning status
            """,
            st["id"],
            error,
            NOT_RETRIED,
        )
        if r is not None:
            await events.emit(
                "subtask.status", f"{st['title']}: {'will retry' if r['status'] == 'queued' else 'failed'} — {error[:300]}",
                task_id=st["task_id"], subtask_id=st["id"], level="warn", data={"status": r["status"]},
            )

    async def _charge(self, st: dict, ctx: RunContext) -> None:
        u = ctx.usage
        if not (u.input_tokens or u.output_tokens or u.cost_usd):
            return
        p = await db.pool()
        await p.execute(
            "update subtasks set input_tokens = input_tokens + $2, output_tokens = output_tokens + $3, cost_usd = cost_usd + $4 where id = $1",
            st["id"], u.input_tokens, u.output_tokens, u.cost_usd,
        )
        await p.execute(
            "update tasks set input_tokens = input_tokens + $2, output_tokens = output_tokens + $3, cost_usd = cost_usd + $4 where id = $1",
            st["task_id"], u.input_tokens, u.output_tokens, u.cost_usd,
        )
