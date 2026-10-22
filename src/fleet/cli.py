"""Command line: `fleet serve`, `fleet worker`, `fleet run "..."`, `fleet profiles ...` (sync, add-local, ...), `fleet migrate`."""

import argparse
import asyncio
import logging
import sys

from fleet.config import settings  # noqa: F401  (sets browser-use env vars before anything imports it)


def _logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-5s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    for noisy in ("httpx", "httpcore", "openai", "browser_use", "cdp_use", "bubus", "scrapling", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def cmd_serve(args) -> None:
    import uvicorn

    from fleet import db
    from fleet.api import create_app

    s = settings()

    async def serve():
        # One event loop for the database check (a readable error before the web server starts) and the server itself,
        # so an embedded Postgres started by the check is used from the loop that started it.
        await db.ensure_database()
        print(f"Fleet panel: http://{s.host}:{s.port}", flush=True)
        config = uvicorn.Config(create_app(run_background=not args.api_only), host=s.host, port=s.port, log_level="warning")
        await uvicorn.Server(config).serve()

    asyncio.run(serve())


async def _background(with_conductor: bool, stop: asyncio.Event) -> None:
    from fleet import db
    from fleet.conductor import Conductor
    from fleet.worker import Worker

    await db.migrate()
    jobs = [Worker().run_forever(stop)]
    if with_conductor:
        jobs.append(Conductor().run_forever(stop))
    await asyncio.gather(*jobs)


def cmd_worker(args) -> None:
    stop = asyncio.Event()
    try:
        asyncio.run(_background(not args.no_conductor, stop))
    except KeyboardInterrupt:
        pass


async def _run_prompt(args) -> int:
    from fleet import db

    await db.migrate()
    p = await db.pool()
    task = await p.fetchrow(
        "insert into tasks (prompt, max_parallel, require_approval, allowed_domains, budget_usd) "
        "values ($1, $2, $3, $4, $5) returning *",
        args.prompt, args.parallel, not args.no_approval, args.domain or [], args.budget,
    )
    print(f"task {task['id']}")
    stop = asyncio.Event()
    bg = asyncio.create_task(_background(True, stop)) if not args.no_inline else None
    last, asked = 0, None
    try:
        while True:
            for ev in await p.fetch("select * from events where task_id = $1 and id > $2 order by id", task["id"], last):
                last = ev["id"]
                print(f"  [{ev['type']}] {ev['message']}")
            t = await p.fetchrow("select status, round, result, error, cost_usd from tasks where id = $1", task["id"])
            if t["status"] == "awaiting_approval" and asked != t["round"]:
                asked = t["round"]
                await _confirm_plan(task["id"], t["round"], args.yes)
                continue
            if t["status"] in ("done", "failed", "cancelled"):
                print(f"\n=== {t['status'].upper()} (cost ${float(t['cost_usd']):.4f}) ===\n")
                print(t["result"] or t["error"] or "")
                return 0 if t["status"] == "done" else 1
            await asyncio.sleep(1)
    finally:
        stop.set()
        if bg:
            await asyncio.wait([bg], timeout=10)
        await db.close()


async def _confirm_plan(task_id, round_: int, assume_yes: bool) -> None:
    """Show the plan in the terminal and ask before any worker touches an account."""
    from fleet import conductor, db

    p = await db.pool()
    rows = await p.fetch(
        "select s.key, s.kind, s.title, pr.name as profile from subtasks s left join profiles pr on pr.id = s.profile_id "
        "where s.task_id = $1 and s.round = $2 order by s.created_at",
        task_id, round_,
    )
    print("\n  Plan (waiting for your OK):")
    for r in rows:
        print(f"    {r['key']:<4} {r['kind']:<9} {r['title']}" + (f"  [profile: {r['profile']}]" if r["profile"] else ""))
    ok = assume_yes or (await asyncio.to_thread(input, "  Run this plan? [y/N] ")).strip().lower() in ("y", "yes")
    if ok:
        await conductor.approve_plan(task_id)
    else:
        await conductor.cancel_task(task_id)


def cmd_run(args) -> None:
    sys.exit(asyncio.run(_run_prompt(args)))


async def _profiles(args) -> None:
    from fleet import db, profiles

    await db.migrate()
    p = await db.pool()
    try:
        if args.action == "sync":
            print(await profiles.sync_from_adspower())
        elif args.action == "add-local":
            if not args.name:
                raise SystemExit("usage: fleet profiles add-local <name> [--visible]")
            r = await profiles.add_local(args.name, headless=not args.visible, notes=args.text or "")
            print(f"added local profile “{r['name']}” ({'visible' if args.visible else 'headless'}), enabled for general browsing")
        elif args.action in ("enable", "disable"):
            n = await p.execute(
                "update profiles set enabled = $2 where lower(name) = lower($1) or adspower_id = $1",
                args.name, args.action == "enable",
            )
            print(n)
        elif args.action == "note":
            print(await p.execute(
                "update profiles set notes = $2 where lower(name) = lower($1) or adspower_id = $1", args.name, args.text
            ))
        else:
            for r in await p.fetch("select * from profiles order by enabled desc, name"):
                flag = "ON " if r["enabled"] else "off"
                print(f"{flag}  {r['name']:<24} {r.get('kind', 'adspower'):<9} {r['adspower_id']:<18} {r['status']:<6} {r['notes']}")
    finally:
        await db.close()


def cmd_profiles(args) -> None:
    asyncio.run(_profiles(args))


async def _secrets(args) -> None:
    import getpass

    from fleet import db, vault

    await db.migrate()
    try:
        if args.action == "set":
            # Read from a hidden prompt, never from argv (argv lands in shell history and process lists).
            value = getpass.getpass(f"Paste {args.name} (input hidden): ")
            info = await vault.put(args.name, value)
            print(f"saved {info['name']} {info['hint']}")
        elif args.action == "delete":
            print("deleted" if await vault.delete(args.name) else "not found")
        else:
            for s in await vault.list_metadata():
                state = f"set {s.get('hint') or ''}" if s["set"] else "missing" + (" (required)" if s["required"] else "")
                print(f"{s['name']:<24} {state}")
    finally:
        await db.close()


def cmd_secrets(args) -> None:
    asyncio.run(_secrets(args))


def cmd_alias(args) -> None:
    from fleet import db, simplelogin

    async def go() -> int:
        await db.migrate()
        try:
            print(await simplelogin.create(note=args.note or "", hostname=args.hostname or ""))
            return 0
        except simplelogin.AliasError as e:
            print(f"fleet alias: {e}", file=sys.stderr)
            return 1
        finally:
            await db.close()

    raise SystemExit(asyncio.run(go()))


def cmd_migrate(_args) -> None:
    from fleet import db

    async def go():
        applied = await db.migrate()
        await db.close()
        print("applied:", applied or "nothing new")

    asyncio.run(go())


def main() -> None:
    ap = argparse.ArgumentParser(prog="fleet", description="Prompt-driven browser automation on AdsPower.")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("serve", help="API + panel + conductor + workers in one process")
    sp.add_argument("--api-only", action="store_true", help="don't run conductor/workers here")
    sp.set_defaults(fn=cmd_serve)

    sp = sub.add_parser("worker", help="run workers (and a conductor) without the API — e.g. on another machine")
    sp.add_argument("--no-conductor", action="store_true")
    sp.set_defaults(fn=cmd_worker)

    sp = sub.add_parser("run", help="run one prompt from the terminal and print progress")
    sp.add_argument("prompt")
    sp.add_argument("--parallel", type=int, default=4)
    sp.add_argument("--no-approval", action="store_true", help="skip approvals except payments")
    sp.add_argument("--domain", action="append", help="allowed domain (repeatable)")
    sp.add_argument("--budget", type=float, default=2.0, help="max spend in USD for this task (default 2)")
    sp.add_argument("--no-inline", action="store_true", help="rely on an already running `fleet serve`")
    sp.add_argument("--yes", action="store_true", help="run the plan without asking, even if it uses your profiles")
    sp.set_defaults(fn=cmd_run)

    sp = sub.add_parser("profiles", help="manage browser profiles (AdsPower, or local Chrome profiles of Fleet's own)")
    sp.add_argument("action", choices=["list", "sync", "add-local", "enable", "disable", "note"])
    sp.add_argument("name", nargs="?")
    sp.add_argument("text", nargs="?", help="for note: the note; for add-local: an optional note")
    sp.add_argument("--visible", action="store_true", help="add-local: open a window instead of running headless")
    sp.set_defaults(fn=cmd_profiles)

    sp = sub.add_parser("secrets", help="API keys, stored encrypted (values are never printed)")
    sp.add_argument("action", choices=["list", "set", "delete"])
    sp.add_argument("name", nargs="?", help="e.g. OPENROUTER_API_KEY")
    sp.set_defaults(fn=cmd_secrets)

    sp = sub.add_parser("alias", help="SimpleLogin email aliases for sign-ups")
    sp.add_argument("action", choices=["new"])
    sp.add_argument("--note", help="what the alias is for, kept on SimpleLogin's side")
    sp.add_argument("--hostname", help="the site it is for")
    sp.set_defaults(fn=cmd_alias)

    sp = sub.add_parser("migrate", help="create/upgrade the database")
    sp.set_defaults(fn=cmd_migrate)

    args = ap.parse_args()
    _logging(args.verbose)
    from fleet.db import DatabaseUnavailable

    try:
        args.fn(args)
    except DatabaseUnavailable as e:
        print(f"\nFleet can't start: {e}", file=sys.stderr)
        sys.exit(1)
