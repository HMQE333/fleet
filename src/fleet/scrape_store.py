"""Where scraped items are kept, and how the operator gets them out."""

import json
import re
from uuid import UUID

from fleet import db, scrape


async def save(subtask_id: UUID, *, names: list[str], items: list[dict], recipe: dict, pages: int, reused: bool,
               more_pages: bool, changes: dict | None = None, alerts: list[dict] | None = None) -> None:
    p = await db.pool()
    await p.execute(
        """
        insert into scrape_results (subtask_id, fields, items, recipe, pages, reused, more_pages, changes, alerts)
        values ($1, $2, $3, $4, $5, $6, $7, $8, $9)
        on conflict (subtask_id) do update set fields = excluded.fields, items = excluded.items,
            recipe = excluded.recipe, pages = excluded.pages, reused = excluded.reused,
            more_pages = excluded.more_pages, changes = excluded.changes, alerts = excluded.alerts, seen = false,
            updated_at = now()
        """,
        subtask_id, names, items, recipe, pages, reused, more_pages, changes, alerts or None,
    )


async def previous(subtask: dict) -> dict | None:
    """The result of the same step in the automation's previous run (for the first run: in the task it was saved
    from), if this subtask belongs to a saved automation's run. Plain tasks have nothing to compare with."""
    p = await db.pool()
    return db.row(await p.fetchrow(
        """
        select r.fields, r.items, r.more_pages, r.updated_at
        from tasks t
        join automations a on a.id = t.automation_id
        join tasks earlier on (earlier.automation_id = a.id or earlier.id = a.source_task_id)
            and earlier.created_at < t.created_at
        join subtasks s on s.task_id = earlier.id and s.key = $2
        join scrape_results r on r.subtask_id = s.id
        where t.id = $1
        order by earlier.created_at desc limit 1
        """,
        subtask["task_id"], subtask["key"]))


async def get(subtask_id: UUID) -> dict | None:
    p = await db.pool()
    return db.row(await p.fetchrow(
        "select r.*, s.title from scrape_results r join subtasks s on s.id = r.subtask_id where r.subtask_id = $1",
        subtask_id))


UPDATES_SQL = """
    select r.subtask_id as id, s.task_id, s.title, a.id as automation_id, a.name as automation_name,
           r.changes -> 'counts' as counts, coalesce((r.changes ->> 'partial')::boolean, false) as partial,
           coalesce(r.alerts, '[]'::jsonb) as alerts, jsonb_array_length(r.items) as rows, r.updated_at
    from scrape_results r
    join subtasks s on s.id = r.subtask_id
    join tasks t on t.id = s.task_id
    join automations a on a.id = t.automation_id
    where not r.seen and (
        (r.changes is not null and coalesce((r.changes ->> 'comparable')::boolean, false)
         and ((r.changes -> 'counts' ->> 'added')::int + (r.changes -> 'counts' ->> 'removed')::int
              + (r.changes -> 'counts' ->> 'changed')::int) > 0)
        or jsonb_array_length(coalesce(r.alerts, '[]'::jsonb)) > 0)
    order by r.updated_at desc limit 50
"""


async def updates() -> list[dict]:
    """Scrapes of saved automations that found changes or crossed a watch line and haven't been marked as seen (newest first)."""
    p = await db.pool()
    return db.rows(await p.fetch(UPDATES_SQL))


async def mark_seen(subtask_id: UUID | None = None) -> int:
    """Mark one update (or, with no id, all of them) as seen. Returns how many changed."""
    p = await db.pool()
    if subtask_id is None:
        done = await p.execute("update scrape_results set seen = true where not seen and (changes is not null or alerts is not null)")
    else:
        done = await p.execute("update scrape_results set seen = true where subtask_id = $1 and not seen", subtask_id)
    return int(done.rsplit(" ", 1)[1])


def filename(title: str, ext: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:50] or "scrape"
    return f"{slug}.{ext}"


def as_csv(result: dict) -> str:
    return scrape.to_csv(result["fields"], result["items"])


def as_json(result: dict) -> str:
    return json.dumps({"fields": result["fields"], "count": len(result["items"]), "items": result["items"]},
                      ensure_ascii=False, indent=1)
