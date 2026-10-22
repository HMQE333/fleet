"""Profiles (AdsPower, synced; or local, created here) and their leases: one worker drives one profile at a time (RULES.md §5).

A lease is a row lock turned into a timestamp. The holder renews it with every
heartbeat; if a worker dies, the lease expires and the reaper frees the profile.
"""

from uuid import UUID, uuid4

import asyncpg

from fleet import adspower, db
from fleet.config import settings


async def sync_from_adspower() -> dict:
    """Mirror AdsPower's profile list. New profiles arrive disabled."""
    remote = await adspower.client().list_profiles()
    p = await db.pool()
    added = 0
    async with p.acquire() as conn:
        for prof in remote:
            status = await conn.execute(
                """
                insert into profiles (adspower_id, name, group_name) values ($1, $2, $3)
                on conflict (adspower_id) do update set name = excluded.name, group_name = excluded.group_name
                """,
                str(prof.get("user_id")),
                prof.get("name") or str(prof.get("serial_number") or prof.get("user_id")),
                prof.get("group_name") or "",
            )
            added += status == "INSERT 0 1"
    return {"seen": len(remote), "added": added}


async def add_local(name: str, *, headless: bool = True, general_use: bool = True, notes: str = "") -> dict:
    """A local Chrome / Edge profile Fleet starts itself (fleet/browsers.py): enabled at once, with its own user data
    directory under data/profiles/<id>. Headless by default; "Open" on the Profiles page shows its window when the
    operator wants to log in by hand."""
    name = name.strip()
    if not name:
        raise ValueError("A profile needs a name")
    p = await db.pool()
    r = await p.fetchrow(
        """
        insert into profiles (adspower_id, name, kind, headless, enabled, general_use, notes)
        values ($1, $2, 'local', $3, true, $4, $5) returning *
        """,
        f"local:{uuid4().hex[:12]}", name, headless, general_use, notes,
    )
    return dict(r)


async def try_lease(conn: asyncpg.Connection, owner: str, profile_id: UUID | None) -> dict | None:
    """Lease an idle enabled profile: the named one, or else one marked for general browsing.

    Call inside a transaction. An unnamed subtask never lands on an account profile.
    """
    r = await conn.fetchrow(
        f"""
        update profiles set status = 'busy', lease_owner = $1,
            lease_expires_at = now() + interval '{settings().lease_seconds} seconds', last_used_at = now()
        where id = (
            select id from profiles
            where enabled and status = 'idle' and (id = $2 or ($2::uuid is null and general_use))
            order by last_used_at nulls first
            for update skip locked
            limit 1
        )
        returning *
        """,
        owner,
        profile_id,
    )
    return db.row(r)


async def renew(profile_id: UUID, owner: str) -> None:
    p = await db.pool()
    await p.execute(
        f"update profiles set lease_expires_at = now() + interval '{settings().lease_seconds} seconds' "
        "where id = $1 and lease_owner = $2",
        profile_id,
        owner,
    )


async def release(profile_id: UUID, owner: str, error: str | None = None) -> None:
    p = await db.pool()
    await p.execute(
        """
        update profiles set status = case when $3::text is null then 'idle' else 'error' end,
            last_error = coalesce($3, last_error), lease_owner = null, lease_expires_at = null
        where id = $1 and lease_owner = $2
        """,
        profile_id,
        owner,
        error,
    )


async def reap_expired() -> int:
    p = await db.pool()
    status = await p.execute(
        "update profiles set status = 'idle', lease_owner = null, lease_expires_at = null "
        "where status = 'busy' and lease_expires_at < now()"
    )
    return int(status.split()[-1])
