"""Postgres access: one asyncpg pool per process, plain SQL, file-based migrations."""

import json
import logging
from importlib import resources

import asyncpg

from fleet.config import settings

log = logging.getLogger("fleet.db")

_pool: asyncpg.Pool | None = None


class DatabaseUnavailable(RuntimeError):
    pass


def _where(dsn: str) -> str:
    """host:port/db from a DSN, without the password."""
    from urllib.parse import urlsplit

    u = urlsplit(dsn)
    return f"{u.hostname}:{u.port or 5432}{u.path}"


async def _init_connection(conn: asyncpg.Connection) -> None:
    for typ in ("json", "jsonb"):
        await conn.set_type_codec(typ, encoder=json.dumps, decoder=json.loads, schema="pg_catalog")


EMBEDDED = "embedded"
_embedded_dsn: str | None = None


def _start_embedded() -> str:
    """Fleet's own Postgres under data/pg (the pgserver package), started once per process, stopped when it exits."""
    try:
        import pgserver
    except ImportError as e:
        raise DatabaseUnavailable(
            "FLEET_DATABASE_URL=embedded needs the pgserver package: `pip install pgserver` (or `uv sync --extra embedded`), "
            "or point FLEET_DATABASE_URL at a Postgres server."
        ) from e
    from urllib.parse import urlsplit, urlunsplit

    pgdata = settings().data_dir / "pg"
    pgdata.mkdir(parents=True, exist_ok=True)
    try:
        server = pgserver.get_server(str(pgdata))
    except Exception as e:  # pgserver raises plain exceptions for a locked or broken data directory
        raise DatabaseUnavailable(f"The embedded Postgres under {pgdata} couldn't start: {e}") from e
    u = urlsplit(server.get_uri())
    dsn = urlunsplit((u.scheme, u.netloc, "/fleet", u.query, u.fragment))
    log.info("embedded Postgres up under %s", pgdata)
    return dsn


async def dsn() -> str:
    """The DSN to connect to: FLEET_DATABASE_URL, or the embedded server's address when it says "embedded"."""
    global _embedded_dsn
    url = settings().database_url
    if url != EMBEDDED:
        return url
    if _embedded_dsn is None:
        import asyncio

        _embedded_dsn = await asyncio.to_thread(_start_embedded)
    return _embedded_dsn


async def pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(await dsn(), min_size=2, max_size=20, init=_init_connection)
    return _pool


async def close() -> None:
    """Close the pool. A connection a stray task still holds must not keep the next pool from being created (tests run
    hundreds of pools, one per event loop): after a short grace period the pool is terminated instead."""
    global _pool
    p, _pool = _pool, None
    if p is None:
        return
    import asyncio

    try:
        await asyncio.wait_for(p.close(), timeout=5)
    except (TimeoutError, asyncio.CancelledError, Exception) as e:  # noqa: BLE001 - closing must never fail the caller
        log.debug("pool close did not finish cleanly (%s); terminating", e)
        p.terminate()


async def ensure_database() -> None:
    """Create the target database if it doesn't exist yet (local convenience)."""
    url = await dsn()
    try:
        conn = await asyncpg.connect(url, timeout=10)
    except (OSError, TimeoutError) as e:
        raise DatabaseUnavailable(
            f"Can't reach Postgres at {_where(url)} ({e.__class__.__name__}). Is the Postgres service running, "
            "and is FLEET_DATABASE_URL in .env right?"
        ) from e
    except (asyncpg.InvalidPasswordError, asyncpg.InvalidAuthorizationSpecificationError) as e:
        raise DatabaseUnavailable(f"Postgres at {_where(url)} rejected the login. Check the user and password in FLEET_DATABASE_URL (.env).") from e
    except asyncpg.InvalidCatalogNameError:
        base, _, dbname = url.rpartition("/")
        dbname = dbname.split("?")[0]
        admin = await asyncpg.connect(f"{base}/postgres")
        try:
            await admin.execute(f'create database "{dbname}"')
            log.info("created database %s", dbname)
        finally:
            await admin.close()
    else:
        await conn.close()


async def migrate() -> list[str]:
    """Apply every migrations/*.sql file that hasn't run yet, in name order."""
    await ensure_database()
    p = await pool()
    applied_now = []
    async with p.acquire() as conn:
        await conn.execute(
            "create table if not exists schema_migrations (version text primary key, applied_at timestamptz not null default now())"
        )
        done = {r["version"] for r in await conn.fetch("select version from schema_migrations")}
        files = sorted(f for f in resources.files("fleet.migrations").iterdir() if f.name.endswith(".sql"))
        for f in files:
            if f.name in done:
                continue
            async with conn.transaction():
                await conn.execute(f.read_text(encoding="utf-8"))
                await conn.execute("insert into schema_migrations (version) values ($1)", f.name)
            applied_now.append(f.name)
            log.info("applied migration %s", f.name)
    return applied_now


def row(r: asyncpg.Record | None) -> dict | None:
    return dict(r) if r is not None else None


def rows(rs: list[asyncpg.Record]) -> list[dict]:
    return [dict(r) for r in rs]
