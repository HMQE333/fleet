import asyncio
import functools
import http.server
import json
import os
import pathlib
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import urllib.request

import pytest

TEST_DB = os.environ.get("FLEET_TEST_DATABASE_URL")
if TEST_DB:
    os.environ["FLEET_DATABASE_URL"] = TEST_DB

# Tests must never touch the operator's real OS credential store or real API keys.
os.environ["FLEET_MASTER_KEY"] = "dGVzdC1tYXN0ZXIta2V5LTMyLWJ5dGVzLWxvbmcuLi4="  # 32 bytes, tests only
os.environ["OPENROUTER_API_KEY"] = ""  # set (empty) so a real .env can't fill it in

from contextlib import asynccontextmanager  # noqa: E402

from fleet import conductor, db, planner, worker  # noqa: E402
from fleet.executors import Blocked, ExecResult  # noqa: E402
from fleet.planner import Plan, PlannedSubtask, Review  # noqa: E402


def pytest_configure(config):
    config.addinivalue_line("markers", "no_db: test doesn't need Postgres")


def pytest_sessionstart(session):
    """Start every run from an empty database, so an old schema can never mask or fake a failure."""
    if not TEST_DB:
        return
    import asyncpg

    base, _, name = TEST_DB.rpartition("/")

    async def recreate():
        admin = await asyncpg.connect(f"{base}/postgres")
        try:
            await admin.execute(f'drop database if exists "{name}" with (force)')
            await admin.execute(f'create database "{name}"')
        finally:
            await admin.close()

    asyncio.run(recreate())


def pytest_collection_modifyitems(config, items):
    if TEST_DB:
        return
    skip = pytest.mark.skip(reason="set FLEET_TEST_DATABASE_URL to run database tests")
    for item in items:
        if not item.get_closest_marker("no_db"):
            item.add_marker(skip)


@pytest.fixture(autouse=True)
async def clean_db(request):
    if request.node.get_closest_marker("no_db"):
        yield
        return
    await db.migrate()
    p = await db.pool()
    await p.execute("truncate events, subtasks, tasks, profiles, automations, settings, secrets, site_accounts, scrape_results, trust_rules cascade")
    yield
    await db.close()


async def wait_for(predicate, timeout: float = 20.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if result := await predicate():
            return result
        await asyncio.sleep(0.2)
    raise AssertionError("timed out waiting for condition")


# -- shared pipeline helpers: stand-in planner/executors and task utilities -------------


def sub(key, kind="fetch", depends_on=(), profile=None, url="https://example.com"):
    return PlannedSubtask(
        key=key, title=f"Do {key}", kind=kind, instructions=f"instructions for {key}", start_url=url,
        depends_on=list(depends_on), profile=profile, model_tier="fast",
    )


@asynccontextmanager
async def running_fleet():
    """A test that fails or times out inside this block must not leave anything of the conductor/worker loops
    running: `run_forever` cancels its own spawned steps but doesn't wait for them, so a slow one (a real browser
    step stuck on a flaky page) is still unwinding after `run_forever` itself has already returned. Anything still
    on the loop once `stop` has had its grace period is cancelled and awaited here, so it can never outlive this
    event loop and reuse (or recreate) the global db pool on a loop pytest-asyncio is about to close."""
    stop = asyncio.Event()
    jobs = [
        asyncio.create_task(conductor.Conductor().run_forever(stop)),
        asyncio.create_task(worker.Worker().run_forever(stop)),
    ]
    this_task = asyncio.current_task()
    try:
        yield
    finally:
        stop.set()
        await asyncio.wait(jobs, timeout=10)
        leftover = [t for t in asyncio.all_tasks() if t is not this_task and not t.done()]
        for t in leftover:
            t.cancel()
        if leftover:
            await asyncio.wait(leftover, timeout=5)


async def new_task(prompt="do the thing", plan_review="never"):
    """Most tests aren't about plan review, so they skip it; the review tests pass "auto"."""
    p = await db.pool()
    return dict(await p.fetchrow(
        "insert into tasks (prompt, plan_review) values ($1, $2) returning *", prompt, plan_review))


async def task_status(task_id):
    p = await db.pool()
    return await p.fetchrow("select * from tasks where id = $1", task_id)


async def finished(task_id):
    t = await task_status(task_id)
    return t if t["status"] in ("done", "failed", "cancelled") else None


async def subtasks(task_id):
    p = await db.pool()
    return {r["key"]: dict(r) for r in await p.fetch("select * from subtasks where task_id = $1", task_id)}


@pytest.fixture
def fake(monkeypatch):
    """Configurable stand-ins for the planner and executors."""

    class Fake:
        plan = Plan(decision="plan", reason="", summary="test plan", subtasks=[])
        reviews: list[Review] = []
        seen_deps: dict[str, dict] = {}
        fail_keys: set[str] = set()
        max_concurrent = 0
        _running = 0

    f = Fake()

    async def fake_plan(task, profiles, usage):
        return f.plan

    async def fake_review(task, subs, profiles, usage, last_round):
        if f.reviews:
            return f.reviews.pop(0)
        done = sorted(s["key"] for s in subs if s["status"] == "done")
        return Review(decision="finish", answer=f"done: {','.join(done)}", subtasks=[])

    async def fake_exec(ctx):
        key = ctx.subtask["key"]
        f.seen_deps[key] = ctx.dependency_results
        f._running += 1
        f.max_concurrent = max(f.max_concurrent, f._running)
        try:
            await asyncio.sleep(0.3)
            if key in f.fail_keys:
                raise Blocked("site said no")
            return ExecResult(result=f"result of {key}")
        finally:
            f._running -= 1

    monkeypatch.setattr(planner, "plan", fake_plan)
    monkeypatch.setattr(planner, "review", fake_review)
    for kind in ("fetch", "research", "browser"):
        monkeypatch.setitem(worker.EXECUTORS, kind, fake_exec)
    return f


# -- real-browser fixtures for the fast browser (skipped without Edge/Chrome) -----------------

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "fast"
BROWSERS = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    "/usr/bin/google-chrome", "/usr/bin/chromium", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def site():
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(FIXTURES))
    handler.log_message = lambda *a: None
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


@pytest.fixture(scope="module")
def browser_ws():
    exe = next((b for b in BROWSERS if pathlib.Path(b).exists()), None)
    if exe is None:
        pytest.skip("no Edge/Chrome installed")
    port, profile = _free_port(), tempfile.mkdtemp(prefix="fleet-fast-")
    proc = subprocess.Popen([exe, "--headless=new", f"--remote-debugging-port={port}", f"--user-data-dir={profile}",
                             "--no-first-run", "--disable-gpu", "about:blank"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(100):
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=1) as r:
                    yield json.load(r)["webSocketDebuggerUrl"]
                    break
            except OSError:
                time.sleep(0.1)
        else:
            pytest.fail("browser didn't start")
    finally:
        proc.terminate()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:  # Edge sometimes takes longer than that to leave; a slow exit is not a failed test
            proc.kill()
            proc.wait(10)
        shutil.rmtree(profile, ignore_errors=True)


def scripted(script: list, calls: list):
    """A stand-in for Jev: each entry is (operation, label), 'DONE' or 'BLOCKED'."""

    async def choose(page, goal, history, *, model, usage, hint=None):
        calls.append(hint)
        entry = script.pop(0)
        if entry in ("DONE", "BLOCKED"):
            return {"choice": entry, "operation": entry, "probabilities": {}, "goal_done": 0.0, "stuck": 0.0,
                    "confidence": 1.0}
        kind, label = entry
        action = next(a for a in page["actions"] if a["kind"] == kind and a["label"] == label)
        return {"choice": action["id"], "operation": kind.upper(), "probabilities": {action["id"]: 0.9},
                "goal_done": 0.0, "stuck": 0.0, "confidence": 0.9}

    return choose
