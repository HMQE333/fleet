"""Reading and pruning the experience base (READINESS.md, gate K4): the operator can see what Fleet learned about a
site and make it forget an entry. `fleet.experience` writes the rows; this module only reads and deletes them.

What goes out is what was written: the step's goal in the operator's words, the replayable path (labels and typed
values, which the page reader never takes from password, card or code fields), the outcome, its redacted one-line
reason, cost, time and the task it belonged to. No page text, no model prose and no secret ever reach the table,
so every column may be shown.
"""

from __future__ import annotations

from uuid import UUID

from fleet import db

LIMIT = 200
"""At most this many entries per listing: newest first, the rest is history the operator doesn't need on one page."""

COLUMNS = "id, domain, goal, kind, status, reason, actions, cost_usd, seconds, task_id, subtask_id, created_at"


async def entries(domain: str | None = None, limit: int = LIMIT) -> list[dict]:
    """What Fleet learned, newest first; for one site when `domain` is given (matched exactly, lower-case)."""
    limit = max(1, min(int(limit), LIMIT))
    p = await db.pool()
    if domain:
        rows = await p.fetch(
            f"select {COLUMNS} from experience where domain = $1 order by created_at desc, seq desc limit $2",
            domain.strip().lower(), limit)
    else:
        rows = await p.fetch(f"select {COLUMNS} from experience order by created_at desc, seq desc limit $1", limit)
    return db.rows(rows)


async def sites() -> list[dict]:
    """Every site Fleet has been to, most visited first: how many entries, how many of them ended well, when last."""
    p = await db.pool()
    return db.rows(await p.fetch(
        """
        select domain, count(*) as count,
               count(*) filter (where status in ('done', 'goal_achieved')) as succeeded,
               coalesce(sum(cost_usd), 0) as cost_usd, max(created_at) as last_at
        from experience group by domain order by count desc, domain
        """
    ))


async def forget(entry_id: UUID) -> bool:
    """Remove one entry. False when there was none: the operator may have pressed Forget twice."""
    p = await db.pool()
    return (await p.execute("delete from experience where id = $1", entry_id)) == "DELETE 1"
