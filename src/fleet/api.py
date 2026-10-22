"""HTTP API for the control panel, plus a live event stream (SSE backed by Postgres NOTIFY)."""

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import asyncpg
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from fleet import (
    adspower, automations, browsers, conductor, db, hitl, identity, limits, models, notify, openrouter, profiles,
    providers, retention, schedules, scrape_store, site_accounts, trust, vault, watches,
)
from fleet import costs_report, events, experience_api, simplelogin, voice
from fleet.config import settings

log = logging.getLogger("fleet.api")

WEB_DIST = Path(__file__).resolve().parents[2] / "web" / "dist"


# -- live events --------------------------------------------------------------


class Broadcaster:
    """One LISTEN connection per API process, fanned out to every open SSE stream."""

    def __init__(self):
        self._subs: set[tuple[asyncio.Queue, UUID | None]] = set()
        self._conn: asyncpg.Connection | None = None

    async def start(self) -> None:
        self._conn = await asyncpg.connect(await db.dsn())
        await self._conn.add_listener("fleet_events", self._on_notify)

    async def stop(self) -> None:
        if self._conn is not None:
            await self._conn.close()

    def _on_notify(self, _conn, _pid, _channel, payload: str) -> None:
        asyncio.get_running_loop().create_task(self._fan_out(json.loads(payload)))

    async def _fan_out(self, note: dict) -> None:
        if not self._subs:
            return
        p = await db.pool()
        ev = db.row(await p.fetchrow("select * from events where id = $1", note["id"]))
        if ev is None:
            return
        data = json.dumps(jsonable_encoder(ev))
        for q, task_id in list(self._subs):
            if task_id is None or ev["task_id"] == task_id:
                q.put_nowait(data)

    def subscribe(self, task_id: UUID | None) -> tuple[asyncio.Queue, tuple]:
        q: asyncio.Queue = asyncio.Queue(maxsize=1000)
        key = (q, task_id)
        self._subs.add(key)
        return q, key

    def unsubscribe(self, key: tuple) -> None:
        self._subs.discard(key)


broadcaster = Broadcaster()


# -- app ------------------------------------------------------------------------


def create_app(run_background: bool = True) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await db.migrate()
        await broadcaster.start()
        stop = asyncio.Event()
        background = []
        if run_background:
            from fleet.worker import Worker

            background = [
                asyncio.create_task(conductor.Conductor().run_forever(stop)),
                asyncio.create_task(Worker().run_forever(stop)),
            ]
        yield
        stop.set()
        for t in background:
            try:
                await asyncio.wait_for(t, timeout=10)
            except (TimeoutError, asyncio.CancelledError):
                t.cancel()
        await broadcaster.stop()
        await db.close()

    app = FastAPI(title="Fleet", version="0.1.0", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.middleware("http")(_guard_unsafe_requests)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_request: Request, exc: RequestValidationError):
        # FastAPI echoes the rejected input by default; for passwords and API keys that would echo the secret.
        detail = [{k: v for k, v in e.items() if k not in ("input", "ctx")} for e in exc.errors()]
        return JSONResponse({"detail": detail}, status_code=422)

    # Last-added middleware runs first: reject requests addressed to any other hostname (DNS rebinding).
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=_allowed_hosts())
    _routes(app)
    # StaticFiles refuses paths that escape its directory; the panel uses #/ routes, so no SPA fallback is needed.
    app.mount("/files", StaticFiles(directory=settings().data_dir, check_dir=False), name="files")
    if WEB_DIST.exists():
        app.mount("/", StaticFiles(directory=WEB_DIST, html=True), name="panel")
    return app


# -- request guards --------------------------------------------------------------
#
# The API drives logged-in browsers, so a random website must not be able to talk to it:
#  - Host allowlist: blocks DNS-rebinding pages that would otherwise count as same-origin.
#  - Changes (POST/PATCH/PUT/DELETE) must carry X-Fleet-Client. Browsers only let a foreign page
#    send a custom header after a CORS preflight, which this API never approves.
#  - A foreign Origin on a change is rejected outright.

UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
CLIENT_HEADER = "x-fleet-client"


def _allowed_hosts() -> list[str]:
    extra = [h.strip() for h in settings().allowed_hosts.split(",") if h.strip()]
    return ["127.0.0.1", "localhost", "[::1]", *extra]


def _token_ok(request: Request) -> bool:
    token = settings().api_token
    if not token:
        return True
    return request.headers.get("authorization", "") == f"Bearer {token}" or request.query_params.get("token") == token


async def _guard_unsafe_requests(request: Request, call_next):
    # Screenshots show logged-in pages: when a token is set, they need it too (img tags pass it as ?token=).
    if request.url.path.startswith("/files/") and not _token_ok(request):
        return JSONResponse({"detail": "Missing or wrong API token"}, status_code=401)
    if request.method in UNSAFE_METHODS:
        if not request.headers.get(CLIENT_HEADER):
            return JSONResponse({"detail": "Missing X-Fleet-Client header"}, status_code=403)
        origin = request.headers.get("origin")
        if origin and urlsplit(origin).hostname not in {h.strip("[]") for h in _allowed_hosts()}:
            return JSONResponse({"detail": "Cross-site request refused"}, status_code=403)
    return await call_next(request)


async def require_token(request: Request) -> None:
    if not _token_ok(request):
        raise HTTPException(401, "Missing or wrong API token")


class TaskIn(BaseModel):
    prompt: str = Field(min_length=3, max_length=20000)
    max_parallel: int = Field(default=4, ge=1, le=32)
    max_rounds: int = Field(default=3, ge=1, le=6)
    require_approval: bool = True
    allowed_domains: list[str] = []
    budget_usd: float = Field(default=2.0, gt=0, le=500)
    plan_review: Literal["auto", "always", "never"] = "auto"
    allow_deep_research: bool = False
    allow_registration: bool = False
    use_trust_rules: bool = True  # False: every click that needs approval asks, whatever "always allow" rules exist


class AutomationIn(BaseModel):
    task_id: UUID
    name: str = Field(default="", max_length=120)
    schedule: dict | None = None
    timezone: str = "UTC"


class AutomationPatch(BaseModel):
    name: str | None = Field(default=None, max_length=120)
    enabled: bool | None = None
    schedule: dict | None = None
    clear_schedule: bool = False
    timezone: str | None = None
    require_approval: bool | None = None
    budget_usd: float | None = Field(default=None, gt=0, le=500)


def _automation_view(a: dict) -> dict:
    return {**a, "schedule_text": schedules.describe(a["schedule"], a["timezone"])}


class SiteAccountIn(BaseModel):
    domain: str = Field(min_length=3, max_length=300)
    username: str = Field(min_length=1, max_length=320)
    password: str = Field(min_length=1, max_length=1024)
    profile_id: UUID | None = None
    label: str = Field(default="", max_length=80)
    auto_login: bool = False


class SiteAccountPatch(BaseModel):
    username: str | None = Field(default=None, min_length=1, max_length=320)
    password: str | None = Field(default=None, min_length=1, max_length=1024)
    label: str | None = Field(default=None, max_length=80)
    auto_login: bool | None = None


class IdentityIn(BaseModel):
    first_name: str = Field(default="", max_length=100)
    last_name: str = Field(default="", max_length=100)
    email: str = Field(default="", max_length=320)
    username: str = Field(default="", max_length=100)


class WatchIn(BaseModel):
    step_key: str = Field(min_length=1, max_length=80)
    column: str = Field(min_length=1, max_length=60)
    op: Literal["<", "<=", ">", ">="]
    threshold: float


class NotifyIn(BaseModel):
    url: str | None = Field(default=None, max_length=2000)  # null keeps the saved address, "" removes it
    on: list[str] = Field(default_factory=list, max_length=10)
    format: Literal["json", "plain"] = "json"


class DomainIn(BaseModel):
    domain: str = Field(min_length=3, max_length=253)


class LimitsIn(BaseModel):
    values: dict[str, int | None]  # a number sets the limit, null goes back to the default


class FastBrowserIn(BaseModel):
    enabled: bool


class SecretIn(BaseModel):
    value: str = Field(min_length=1, max_length=8192)


class RespondIn(BaseModel):
    decision: Literal["done", "approve", "reject"]
    note: str = ""
    remember: bool = False  # "approve and always allow this click" (only honoured when the card offered it)


class RespondManyIn(BaseModel):
    ids: list[UUID] = Field(min_length=1, max_length=50)  # exactly the cards the operator was looking at
    decision: Literal["approve", "reject"]
    note: str = Field(default="", max_length=2000)


class LocalProfile(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    headless: bool = True
    general_use: bool = True
    notes: str = ""


class AliasRequest(BaseModel):
    note: str = Field(default="", max_length=200)
    hostname: str = Field(default="", max_length=200)


class ProfilePatch(BaseModel):
    enabled: bool | None = None
    general_use: bool | None = None
    notes: str | None = None
    allowed_domains: list[str] | None = None
    reset: bool = False


class VoiceTaskIn(BaseModel):
    text: str = Field(default="", max_length=20000)  # transcribed words; "new task ..." or the task itself


class VoiceSessionIn(BaseModel):
    url: str = Field(default="about:blank", max_length=2000)
    profile_id: UUID | None = None  # default: the local profile named "Voice" (created when missing)


class VoiceCommandIn(BaseModel):
    session_id: str | None = None
    text: str = Field(default="", max_length=2000)


voice_sessions: dict[str, "voice.Live"] = {}  # session_id -> a live voice tab (gate V2); one process, one operator


def _routes(app: FastAPI) -> None:
    deps = [Depends(require_token)]

    @app.get("/api/health", dependencies=deps)
    async def health():
        p = await db.pool()
        vault_error = None
        try:
            has_key = bool(await vault.get("OPENROUTER_API_KEY"))
        except vault.VaultError as e:
            has_key, vault_error = False, str(e)
        return {
            "db": await p.fetchval("select 1") == 1,
            "adspower": await adspower.client().status(),
            "llm_key": has_key,
            "vault_error": vault_error,
            "models": await models.current(),
        }

    # -- automations -----------------------------------------------------------

    @app.get("/api/automations", dependencies=deps)
    async def list_automations():
        p = await db.pool()
        rows = db.rows(await p.fetch(
            """
            select a.*, t.status as last_status, t.error as last_error
            from automations a left join tasks t on t.id = a.last_task_id order by a.created_at desc
            """
        ))
        return [_automation_view(a) for a in rows]

    @app.get("/api/automations/{automation_id}", dependencies=deps)
    async def get_automation(automation_id: UUID):
        p = await db.pool()
        a = db.row(await p.fetchrow("select * from automations where id = $1", automation_id))
        if a is None:
            raise HTTPException(404, "No such automation")
        runs = db.rows(await p.fetch(
            "select id, status, error, cost_usd, created_at, finished_at from tasks where automation_id = $1 "
            "order by created_at desc limit 20",
            automation_id,
        ))
        return {"automation": _automation_view(a), "runs": runs}

    @app.get("/api/automations/{automation_id}/watches", dependencies=deps)
    async def get_watches(automation_id: UUID):
        try:
            return await watches.overview(automation_id)
        except watches.WatchError as e:
            raise HTTPException(404, str(e)) from e

    @app.post("/api/automations/{automation_id}/watches", dependencies=deps, status_code=201)
    async def add_watch(automation_id: UUID, body: WatchIn):
        try:
            return await watches.add(automation_id, body.step_key, body.column, body.op, body.threshold)
        except watches.WatchError as e:
            raise HTTPException(400, str(e)) from e

    @app.delete("/api/watches/{rule_id}", dependencies=deps)
    async def delete_watch(rule_id: UUID):
        if not await watches.delete(rule_id):
            raise HTTPException(404, "No such watch")
        return {"ok": True}

    @app.post("/api/automations", dependencies=deps, status_code=201)
    async def create_automation(body: AutomationIn):
        try:
            a = await automations.create_from_task(body.task_id, body.name, body.schedule, body.timezone)
        except (automations.AutomationError, schedules.ScheduleError) as e:
            raise HTTPException(400, str(e)) from e
        return _automation_view(a)

    @app.patch("/api/automations/{automation_id}", dependencies=deps)
    async def patch_automation(automation_id: UUID, body: AutomationPatch):
        changes = body.model_dump(exclude_none=True, exclude={"clear_schedule"})
        if body.clear_schedule:
            changes["schedule"] = None
        try:
            a = await automations.update(automation_id, **changes)
        except (automations.AutomationError, schedules.ScheduleError) as e:
            raise HTTPException(400, str(e)) from e
        return _automation_view(a)

    @app.delete("/api/automations/{automation_id}", dependencies=deps)
    async def delete_automation(automation_id: UUID):
        p = await db.pool()
        if (await p.execute("delete from automations where id = $1", automation_id)) != "DELETE 1":
            raise HTTPException(404, "No such automation")
        return {"ok": True}

    @app.post("/api/automations/{automation_id}/run", dependencies=deps, status_code=201)
    async def run_automation(automation_id: UUID):
        try:
            task = await automations.start_run(automation_id, "manual")
        except automations.AutomationError as e:
            raise HTTPException(404, str(e)) from e
        if task is None:
            raise HTTPException(409, "The previous run is still going; runs never overlap")
        return task

    # -- saved logins: passwords are write-only ---------------------------------

    @app.get("/api/site-accounts", dependencies=deps)
    async def list_site_accounts():
        return await site_accounts.list_accounts()

    @app.post("/api/site-accounts", dependencies=deps, status_code=201)
    async def create_site_account(body: SiteAccountIn):
        try:
            return await site_accounts.create(body.domain, body.username, body.password, profile_id=body.profile_id,
                                              label=body.label, auto_login=body.auto_login)
        except (site_accounts.SiteAccountError, vault.VaultError) as e:
            raise HTTPException(400, str(e)) from e

    @app.patch("/api/site-accounts/{account_id}", dependencies=deps)
    async def patch_site_account(account_id: UUID, body: SiteAccountPatch):
        try:
            return await site_accounts.update(account_id, password=body.password, username=body.username,
                                              label=body.label, auto_login=body.auto_login)
        except (site_accounts.SiteAccountError, vault.VaultError) as e:
            raise HTTPException(400, str(e)) from e

    @app.post("/api/site-accounts/{account_id}/reveal", dependencies=deps)
    async def reveal_site_account(account_id: UUID):
        """Show the password of an account Fleet created for the operator. Logged; never cached."""
        try:
            domain, password = await site_accounts.reveal(account_id)
        except (site_accounts.SiteAccountError, vault.VaultError) as e:
            raise HTTPException(400, str(e)) from e
        await events.emit("site_login.revealed", f"The password of the Fleet-created account on {domain} was shown in the panel")
        return JSONResponse({"password": password}, headers={"Cache-Control": "no-store"})

    @app.delete("/api/site-accounts/{account_id}", dependencies=deps)
    async def delete_site_account(account_id: UUID):
        if not await site_accounts.delete(account_id):
            raise HTTPException(404, "No such saved login")
        return {"ok": True}

    # -- trust rules: listed and revoked here, created only from an approved Inbox card ------------------------

    @app.get("/api/trust-rules", dependencies=deps)
    async def list_trust_rules():
        return await trust.list_rules()

    @app.delete("/api/trust-rules/{rule_id}", dependencies=deps)
    async def delete_trust_rule(rule_id: UUID):
        if not await trust.delete(rule_id):
            raise HTTPException(404, "No such rule")
        await events.emit("trust.deleted", "A trust rule was removed in the panel")
        return {"ok": True}

    # -- API keys: write-only ------------------------------------------------

    @app.get("/api/secrets", dependencies=deps)
    async def list_secrets():
        return await vault.list_metadata()

    @app.put("/api/secrets/{name}", dependencies=deps)
    async def put_secret(name: str, body: SecretIn):
        try:
            return await vault.put(name, body.value)
        except vault.VaultError as e:
            raise HTTPException(400, str(e)) from e

    @app.delete("/api/secrets/{name}", dependencies=deps)
    async def delete_secret(name: str):
        if not await vault.delete(name):
            raise HTTPException(404, "No such key")
        return {"ok": True}

    @app.post("/api/secrets/OPENROUTER_API_KEY/check", dependencies=deps)
    async def check_openrouter_key():
        """Ask OpenRouter about the stored key; never echoes the key itself."""
        try:
            info = await openrouter.key_info()
        except openrouter.LLMError as e:
            return {"ok": False, "error": str(e)}
        keep = ("label", "limit", "limit_remaining", "usage", "is_free_tier")
        return {"ok": True, **{k: info.get(k) for k in keep}}

    # -- models --------------------------------------------------------------

    @app.get("/api/settings/models", dependencies=deps)
    async def get_models():
        chosen = await models.current()
        return [
            {"role": role, **{k: spec[k] for k in ("label", "help", "default", "needs")},
             "offcatalog": bool(spec.get("offcatalog")), "model": chosen[role],
             "providers": list(spec.get("providers", (providers.OPENROUTER,)))}
            for role, spec in models.ROLES.items()
        ]

    @app.put("/api/settings/models", dependencies=deps)
    async def put_models(body: dict[str, str]):
        try:
            await models.update(body)
        except models.ModelConfigError as e:
            raise HTTPException(400, str(e)) from e
        return await get_models()

    @app.get("/api/settings/identity", dependencies=deps)
    async def get_identity():
        return await identity.get()

    @app.put("/api/settings/identity", dependencies=deps)
    async def put_identity(body: IdentityIn):
        try:
            return await identity.put(body.model_dump())
        except identity.IdentityError as e:
            raise HTTPException(400, str(e)) from e

    @app.get("/api/maintenance/usage", dependencies=deps)
    async def storage_usage():
        return await retention.usage()

    @app.post("/api/maintenance/prune", dependencies=deps)
    async def prune_now():
        """Clean up now, with the retention period from Settings → Limits (screenshots, histories and step logs only)."""
        days = (await limits.effective()).retention_days
        if days <= 0:
            raise HTTPException(400, "Set how many days to keep first (Settings → Limits). 0 keeps everything.")
        return await retention.prune(days)

    # -- costs -----------------------------------------------------------------

    @app.get("/api/costs/summary", dependencies=deps)
    async def cost_summary(days: int = Query(30, ge=1, le=costs_report.MAX_DAYS)):
        """Spend per role in the last `days` days and first run vs replays per automation (aggregates only)."""
        return await costs_report.summary(days)

    # -- the experience base: what Fleet learned per site, and forgetting -------------------

    @app.get("/api/experience/sites", dependencies=deps)
    async def experience_sites():
        return await experience_api.sites()

    @app.get("/api/experience", dependencies=deps)
    async def experience_entries(domain: str | None = Query(None, max_length=253),
                                 limit: int = Query(experience_api.LIMIT, ge=1, le=experience_api.LIMIT)):
        """Newest first, at most `limit` entries; for one site when `domain` is given."""
        return await experience_api.entries(domain, limit)

    @app.delete("/api/experience/{entry_id}", dependencies=deps)
    async def forget_experience(entry_id: UUID):
        if not await experience_api.forget(entry_id):
            raise HTTPException(404, "No such entry")
        return {"ok": True}

    @app.get("/api/settings/notifications", dependencies=deps)
    async def get_notifications():
        return await notify.config()

    @app.put("/api/settings/notifications", dependencies=deps)
    async def put_notifications(body: NotifyIn):
        try:
            return await notify.save(url=body.url, on=body.on, fmt=body.format)
        except (notify.NotifyError, vault.VaultError) as e:
            raise HTTPException(400, str(e)) from e

    @app.post("/api/settings/notifications/test", dependencies=deps)
    async def test_notification():
        try:
            await notify.send("Fleet test: if you can read this, notifications work.")
        except notify.NotifyError as e:
            raise HTTPException(400, str(e)) from e
        return {"ok": True}

    @app.get("/api/settings/no-register", dependencies=deps)
    async def get_no_register():
        return await identity.blocked_domains()

    @app.post("/api/settings/no-register", dependencies=deps)
    async def add_no_register(body: DomainIn):
        try:
            await identity.block(body.domain)
        except site_accounts.SiteAccountError as e:
            raise HTTPException(400, str(e)) from e
        return await identity.blocked_domains()

    @app.delete("/api/settings/no-register/{domain}", dependencies=deps)
    async def remove_no_register(domain: str):
        if not await identity.unblock(domain):
            raise HTTPException(404, "That site isn't on the list")
        return await identity.blocked_domains()

    @app.get("/api/settings/limits", dependencies=deps)
    async def get_limits():
        return await limits.snapshot()

    @app.put("/api/settings/limits", dependencies=deps)
    async def put_limits(body: LimitsIn):
        try:
            return await limits.put(body.values)
        except limits.LimitError as e:
            raise HTTPException(400, str(e)) from e

    @app.get("/api/settings/fast-browser", dependencies=deps)
    async def get_fast_browser():
        return {"enabled": await models.fast_browser_enabled()}

    @app.put("/api/settings/fast-browser", dependencies=deps)
    async def put_fast_browser(body: FastBrowserIn):
        return {"enabled": await models.set_fast_browser(body.enabled)}

    @app.get("/api/models", dependencies=deps)
    async def model_catalog():
        """The model pickers' list: the OpenRouter catalog, plus the Claude models Fleet can call directly."""
        out = [
            {"id": providers.PREFIX + name, "name": f"{name} (Claude API, your key)", "context": None,
             "input_per_m": per_m_in, "output_per_m": per_m_out,
             # the brain roles need these two, and every Claude model Fleet offers here has them
             "capabilities": ["image", "structured_outputs", "text", "tools"]}
            for name, (per_m_in, per_m_out) in providers.PRICES_PER_M.items()
        ]
        for m in (await openrouter.catalog()).values():
            pricing = m.get("pricing") or {}
            try:
                pin, pout = float(pricing.get("prompt", 0)) * 1e6, float(pricing.get("completion", 0)) * 1e6
            except (TypeError, ValueError):
                pin = pout = 0.0
            out.append({
                "id": m["id"], "name": m.get("name") or m["id"], "context": m.get("context_length"),
                "input_per_m": round(pin, 4), "output_per_m": round(pout, 4),
                "capabilities": sorted(openrouter.capabilities(m)),
            })
        return sorted(out, key=lambda x: x["id"])

    @app.post("/api/tasks", dependencies=deps, status_code=201)
    async def create_task(body: TaskIn):
        p = await db.pool()
        domains = sorted({d.strip().lower() for d in body.allowed_domains if d.strip()})
        r = await p.fetchrow(
            "insert into tasks (prompt, max_parallel, max_rounds, require_approval, allowed_domains, budget_usd, plan_review, "
            "allow_deep_research, allow_registration, use_trust_rules) values ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10) returning *",
            body.prompt.strip(), body.max_parallel, body.max_rounds, body.require_approval, domains, body.budget_usd,
            body.plan_review, body.allow_deep_research, body.allow_registration, body.use_trust_rules,
        )
        return db.row(r)

    @app.get("/api/tasks", dependencies=deps)
    async def list_tasks(limit: int = Query(50, le=200)):
        p = await db.pool()
        rs = await p.fetch(
            """
            select t.id, t.prompt, t.status, t.paused, t.round, t.cost_usd, t.budget_usd, t.estimated_cost, t.created_at, t.finished_at, t.error,
                count(s.id) as subtasks,
                count(s.id) filter (where s.status = 'done') as done,
                count(s.id) filter (where s.status in ('failed', 'skipped')) as failed,
                count(s.id) filter (where s.status = 'running') as running,
                count(s.id) filter (where s.status = 'needs_human') as needs_human
            from tasks t left join subtasks s on s.task_id = t.id
            group by t.id order by t.created_at desc limit $1
            """,
            limit,
        )
        return db.rows(rs)

    @app.get("/api/tasks/{task_id}", dependencies=deps)
    async def get_task(task_id: UUID):
        p = await db.pool()
        task = db.row(await p.fetchrow("select * from tasks where id = $1", task_id))
        if task is None:
            raise HTTPException(404, "No such task")
        subtasks = db.rows(
            await p.fetch(
                """
                select s.*, pr.name as profile_name,
                    (select jsonb_array_length(d.items) from scrape_results d where d.subtask_id = s.id) as data_count,
                    (select d.changes -> 'counts' from scrape_results d where d.subtask_id = s.id) as data_changes
                from subtasks s left join profiles pr on pr.id = s.profile_id
                where s.task_id = $1 order by s.round, s.created_at
                """,
                task_id,
            )
        )
        return {"task": task, "subtasks": subtasks}

    # -- scraped data: view and download -----------------------------------------------

    async def _scrape_result(subtask_id: UUID) -> dict:
        result = await scrape_store.get(subtask_id)
        if result is None:
            raise HTTPException(404, "This step has no scraped data")
        return result

    @app.get("/api/subtasks/{subtask_id}/data", dependencies=deps)
    async def scrape_data(subtask_id: UUID, limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0)):
        r = await _scrape_result(subtask_id)
        return {"fields": r["fields"], "count": len(r["items"]), "pages": r["pages"], "reused": r["reused"],
                "more_pages": r["more_pages"], "changes": r["changes"], "items": r["items"][offset: offset + limit]}

    @app.get("/api/subtasks/{subtask_id}/export.csv", dependencies=deps)
    async def scrape_csv(subtask_id: UUID):
        r = await _scrape_result(subtask_id)
        return Response(scrape_store.as_csv(r), media_type="text/csv; charset=utf-8", headers={
            "Content-Disposition": f'attachment; filename="{scrape_store.filename(r["title"], "csv")}"'})

    @app.get("/api/subtasks/{subtask_id}/export.json", dependencies=deps)
    async def scrape_json(subtask_id: UUID):
        r = await _scrape_result(subtask_id)
        return Response(scrape_store.as_json(r), media_type="application/json", headers={
            "Content-Disposition": f'attachment; filename="{scrape_store.filename(r["title"], "json")}"'})

    @app.get("/api/tasks/{task_id}/events", dependencies=deps)
    async def task_events(task_id: UUID, after: int = 0, limit: int = Query(500, le=2000)):
        p = await db.pool()
        return db.rows(
            await p.fetch(
                "select * from events where task_id = $1 and id > $2 order by id limit $3", task_id, after, limit
            )
        )

    @app.post("/api/tasks/{task_id}/rerun", dependencies=deps, status_code=201)
    async def rerun(task_id: UUID):
        """Start a fresh task with the same prompt and settings (the way to retry a failed or finished task)."""
        p = await db.pool()
        r = await p.fetchrow(
            """
            insert into tasks (prompt, max_parallel, max_rounds, require_approval, allowed_domains, budget_usd, plan_review,
                allow_deep_research, allow_registration, use_trust_rules)
            select prompt, max_parallel, max_rounds, require_approval, allowed_domains, budget_usd, plan_review,
                allow_deep_research, allow_registration, use_trust_rules
            from tasks where id = $1 and status in ('done', 'failed', 'cancelled')
            returning *
            """,
            task_id,
        )
        if r is None:
            raise HTTPException(409, "Only finished tasks can be run again")
        return db.row(r)

    @app.post("/api/tasks/{task_id}/approve", dependencies=deps)
    async def approve(task_id: UUID):
        if not await conductor.approve_plan(task_id):
            raise HTTPException(409, "This task isn't waiting for approval")
        return {"ok": True}

    @app.post("/api/tasks/{task_id}/cancel", dependencies=deps)
    async def cancel(task_id: UUID):
        if not await conductor.cancel_task(task_id):
            raise HTTPException(409, "Task is already finished")
        return {"ok": True}

    @app.post("/api/subtasks/{subtask_id}/retry", dependencies=deps)
    async def retry_step(subtask_id: UUID):
        """Run one failed step of a finished task again (and what was skipped because of it); the rest is left alone."""
        try:
            return await conductor.retry_subtask(subtask_id)
        except conductor.RetryError as e:
            raise HTTPException(409, str(e)) from e

    @app.post("/api/tasks/{task_id}/pause", dependencies=deps)
    async def pause_task(task_id: UUID):
        """Steps already running finish; nothing new starts until the operator resumes."""
        p = await db.pool()
        if not await p.fetchval("update tasks set paused = true where id = $1 and status = 'running' returning true", task_id):
            raise HTTPException(409, "Only a running task can be paused")
        await events.emit("task.paused", "Paused by the operator: steps already running finish, no new steps start",
                          task_id=task_id, level="warn")
        return {"ok": True}

    @app.post("/api/tasks/{task_id}/resume", dependencies=deps)
    async def resume_task(task_id: UUID):
        p = await db.pool()
        if not await p.fetchval("update tasks set paused = false where id = $1 and paused returning true", task_id):
            raise HTTPException(409, "That task isn't paused")
        await events.emit("task.resumed", "Resumed by the operator", task_id=task_id)
        return {"ok": True}

    @app.post("/api/subtasks/{subtask_id}/take-over", dependencies=deps)
    async def take_over_subtask(subtask_id: UUID):
        """Ask a running browser step to pause at its next step so the operator can use the window themselves."""
        p = await db.pool()
        task_id = await p.fetchval(
            "update subtasks set take_over = true where id = $1 and status = 'running' "
            "and kind in ('browser', 'browser_fast') returning task_id", subtask_id)
        if task_id is None:
            raise HTTPException(409, "Only a running browser step can be taken over")
        await events.emit("subtask.take_over", "You asked to take over: the worker pauses at its next step",
                          task_id=task_id, subtask_id=subtask_id, level="warn")
        return {"ok": True}

    @app.get("/api/inbox", dependencies=deps)
    async def inbox():
        """Everything waiting on the operator: plans to okay, paused workers, and (as news, not a question) scrapes
        of saved automations that found changes."""
        p = await db.pool()
        plans = db.rows(await p.fetch(
            """
            select t.id, t.prompt, t.plan_summary, t.round, t.updated_at,
                count(s.id) filter (where s.kind in ('browser', 'browser_fast')) as browser_subtasks, count(s.id) as subtasks
            from tasks t left join subtasks s on s.task_id = t.id and s.round = t.round
            where t.status = 'awaiting_approval' group by t.id order by t.updated_at
            """
        ))
        requests = db.rows(
            await p.fetch(
                """
                select s.id, s.task_id, s.title, s.kind, s.human_request, s.profile_id, t.prompt, pr.name as profile_name
                from subtasks s join tasks t on t.id = s.task_id left join profiles pr on pr.id = s.profile_id
                where s.status = 'needs_human' and s.human_response is null order by s.human_request->>'requested_at'
                """
            )
        )
        return {"plans": plans, "requests": requests, "updates": await scrape_store.updates()}

    @app.post("/api/inbox/updates/seen", dependencies=deps)
    async def mark_all_updates_seen():
        return {"ok": True, "marked": await scrape_store.mark_seen()}

    @app.post("/api/subtasks/{subtask_id}/seen", dependencies=deps)
    async def mark_update_seen(subtask_id: UUID):
        if not await scrape_store.mark_seen(subtask_id):
            raise HTTPException(404, "No unseen update for that step")
        return {"ok": True}

    @app.post("/api/subtasks/{subtask_id}/respond", dependencies=deps)
    async def respond(subtask_id: UUID, body: RespondIn):
        if not await hitl.respond(subtask_id, body.decision, body.note, body.remember):
            raise HTTPException(409, "That subtask isn't waiting for you")
        return {"ok": True}

    @app.post("/api/inbox/respond-many", dependencies=deps)
    async def respond_many(body: RespondManyIn):
        """Answer several waiting cards together (approve or stop). Payments, sign-ups, captchas and "I've handled it"
        answers are never accepted in bulk; they come back in `skipped`."""
        return await hitl.respond_many(body.ids, body.decision, body.note)

    @app.get("/api/profiles", dependencies=deps)
    async def list_profiles():
        p = await db.pool()
        return db.rows(await p.fetch("select * from profiles order by enabled desc, name"))

    @app.post("/api/profiles/sync", dependencies=deps)
    async def sync_profiles():
        try:
            return await profiles.sync_from_adspower()
        except adspower.AdsPowerError as e:
            raise HTTPException(502, str(e)) from e

    @app.post("/api/profiles/local", dependencies=deps, status_code=201)
    async def add_local_profile(body: LocalProfile):
        """A local Chrome / Edge profile Fleet runs itself: no AdsPower needed."""
        return await profiles.add_local(body.name, headless=body.headless, general_use=body.general_use, notes=body.notes)

    @app.patch("/api/profiles/{profile_id}", dependencies=deps)
    async def patch_profile(profile_id: UUID, body: ProfilePatch):
        p = await db.pool()
        r = await p.fetchrow(
            """
            update profiles set enabled = coalesce($2, enabled), notes = coalesce($3, notes),
                general_use = coalesce($6, general_use),
                allowed_domains = coalesce($4, allowed_domains),
                status = case when $5 and status = 'error' then 'idle' else status end,
                last_error = case when $5 then null else last_error end
            where id = $1 returning *
            """,
            profile_id, body.enabled, body.notes,
            None if body.allowed_domains is None else sorted({d.strip().lower() for d in body.allowed_domains if d.strip()}),
            body.reset,
            body.general_use,
        )
        if r is None:
            raise HTTPException(404, "No such profile")
        return db.row(r)

    @app.post("/api/profiles/{profile_id}/open", dependencies=deps)
    async def open_profile(profile_id: UUID):
        """Open the profile's window so the operator can log in or finish a challenge by hand."""
        p = await db.pool()
        prof = await p.fetchrow("select * from profiles where id = $1", profile_id)
        if prof is None:
            raise HTTPException(404, "No such profile")
        try:
            await browsers.open(dict(prof), visible=True)  # a headless local profile gets a window for the operator
        except browsers.BrowserError as e:
            raise HTTPException(502, str(e)) from e
        return {"ok": True}

    @app.post("/api/aliases/random", dependencies=deps, status_code=201)
    async def random_alias(body: AliasRequest):
        """A new random SimpleLogin alias for a sign-up form the operator approved (fleet/simplelogin.py)."""
        try:
            return {"alias": await simplelogin.create(note=body.note, hostname=body.hostname)}
        except simplelogin.AliasError as e:
            raise HTTPException(502, str(e)) from e

    @app.get("/api/stream", dependencies=deps)
    async def stream(request: Request, task_id: UUID | None = None):
        q, key = broadcaster.subscribe(task_id)

        async def gen():
            try:
                yield ": connected\n\n"
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        data = await asyncio.wait_for(q.get(), timeout=15)
                        yield f"data: {data}\n\n"
                    except TimeoutError:
                        yield ": ping\n\n"
            finally:
                broadcaster.unsubscribe(key)

        return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})

    # -- voice (gates V1, V2, V4): spoken words become a task or a command against a live tab ---------------------

    async def _voice_task(text: str) -> dict:
        """A task from transcribed words: "new task X" / "zadanie X" means X; any other sentence is the task itself."""
        cmd = voice.parse(text)
        if cmd is not None and cmd.kind == "refused":
            raise HTTPException(400, cmd.args.get("reason", "Refused"))
        prompt = cmd.args["prompt"] if cmd is not None and cmd.kind == "task" else text.strip()
        if len(prompt) < 3:
            raise HTTPException(400, "Say what the task is")
        task = await create_task(TaskIn(prompt=prompt))
        return {"task_id": task["id"], "prompt": prompt, "said": f"New task: {prompt}"}

    @app.post("/api/voice/task", dependencies=deps, status_code=201)
    async def voice_task(body: VoiceTaskIn):
        return await _voice_task(body.text)

    async def _voice_profile(profile_id: UUID | None) -> dict:
        p = await db.pool()
        if profile_id is not None:
            prof = await p.fetchrow("select * from profiles where id = $1", profile_id)
            if prof is None:
                raise HTTPException(404, "No such profile")
            return dict(prof)
        prof = await p.fetchrow("select * from profiles where kind = 'local' and name = 'Voice' order by created_at limit 1")
        # Its own local profile, not for workers (general_use false): a voice tab never shares a window with a running task.
        return dict(prof) if prof else await profiles.add_local("Voice", headless=False, general_use=False,
                                                                 notes="The operator's voice sessions")

    @app.post("/api/voice/session", dependencies=deps, status_code=201)
    async def voice_session(body: VoiceSessionIn):
        """Open a voice session: a tab in a local Chrome profile the operator can see, driven by /api/voice/command."""
        prof = await _voice_profile(body.profile_id)
        if browsers.kind_of(prof) != browsers.LOCAL:
            raise HTTPException(400, "A voice session needs a local profile")
        try:
            opened = await browsers.open(prof, visible=True)
            live = await voice.Live.connect(opened.cdp_url, body.url, profile_id=str(prof["id"]))
        except browsers.BrowserError as e:
            raise HTTPException(502, str(e)) from e
        except voice.VoiceError as e:
            raise HTTPException(400, str(e)) from e
        except (voice.CDPError, voice.StalePage, OSError, TimeoutError) as e:
            raise HTTPException(502, f"Couldn't open the page: {e}") from e
        session_id = uuid4().hex
        voice_sessions[session_id] = live
        return {"session_id": session_id, "profile_id": str(prof["id"]), "url": body.url}

    @app.post("/api/voice/command", dependencies=deps)
    async def voice_command(body: VoiceCommandIn):
        """Run one spoken command. A refused command (a password, a card number) is 400 and nothing is typed; what the
        grammar doesn't understand is 400 too, so the panel asks again. "new task ..." creates a task without a session."""
        cmd = voice.parse(body.text)
        if cmd is None:
            raise HTTPException(400, voice.describe(None))
        if cmd.kind == "refused":
            raise HTTPException(400, cmd.args.get("reason", "Refused"))
        if cmd.kind == "task":
            return {"command": cmd.to_dict(), "result": {"ok": True, **await _voice_task(body.text)}}
        live = voice_sessions.get(body.session_id or "")
        if live is None:
            raise HTTPException(404, "No such voice session: open one with POST /api/voice/session")
        result = await live.session.execute(cmd)
        return {"command": cmd.to_dict(), "result": result}

    @app.delete("/api/voice/session/{session_id}", dependencies=deps)
    async def close_voice_session(session_id: str):
        live = voice_sessions.pop(session_id, None)
        if live is None:
            raise HTTPException(404, "No such voice session")
        await live.close()
        return {"ok": True}

