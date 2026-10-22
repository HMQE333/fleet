"""Fast browser worker: Jev drives a leased profile (AdsPower or a local Chrome profile); the full agent takes over if it gets stuck.

Same safety contract as the full browser worker (RULES.md §2-4): captchas and logins go to the
operator, irreversible clicks wait for approval (payments always), cancel is honoured every step,
and every step lands in the activity feed with a screenshot. When it was given a recorded path
(an automation replaying a saved browser step) it follows that path first and only asks Jev where
the page changed.
"""

import base64
import logging
import time
from pathlib import Path

from fleet import automations, browsers, click_approval, db, experience, guard, hitl, llm, models, openrouter, prompts
from fleet.registration import Registration
from fleet.saved_login import SavedLogin
from fleet.config import settings
from fleet.executors import ExecResult, RunContext
from fleet.executors import browser as full_browser
from fleet.executors.browser import ProfileError
from fleet.fastbrowser import engine
from fleet.fastbrowser.cdp import CDP, CDPError

log = logging.getLogger("fleet.browser_fast")

# Outcomes the full agent may be able to rescue. Not "rejected"/"stopped": the operator said no.
HAND_OVER = {"stuck", "blocked", "max_steps", "timeout", "error"}


def allowed_domains(task_domains: list[str], profile_domains: list[str]) -> list[str]:
    if task_domains and profile_domains:
        return [d for d in task_domains if any(d == p or d.endswith("." + p) for p in profile_domains)]
    return list(task_domains or profile_domains)


class FleetHooks(engine.Hooks):
    def __init__(self, ctx: RunContext):
        self.ctx = ctx
        self.st = ctx.subtask
        self.profile = ctx.profile or {}
        self.saved_login = SavedLogin(ctx)
        self.registration = Registration(ctx)
        self.shots_dir = settings().screenshots_dir / str(self.st["id"])
        self.shots_dir.mkdir(parents=True, exist_ok=True)

    async def should_stop(self) -> bool:
        p = await db.pool()
        return await p.fetchval("select status from subtasks where id = $1", self.st["id"]) == "cancelled"

    async def on_page(self, state: dict, page) -> str:
        if await hitl.take_over_requested(self.st["id"]):
            await self.ctx.log("You took over this step: paused until you give control back", level="warn")
            resp = await hitl.ask(self.st, "takeover", hitl.take_over_message(self.profile.get("name")), url=state["url"])
            return "stop" if resp.get("decision") == "reject" else "reobserve"
        challenge = await guard.detect_challenge_with(page.evaluate)
        if challenge:
            await self.ctx.log(f"{challenge} detected — pausing for the operator", level="warn")
            resp = await hitl.ask(self.st, "challenge",
                                  f"{challenge} on the page. Please complete it in {browsers.window_name(self.profile)}, "
                                  "then click “I've handled it”.", url=state["url"])
            return "stop" if resp.get("decision") == "reject" else "reobserve"
        async def send(method, params=None):
            return await page.call(method, **(params or {}))

        if not state.get("login") and await self.saved_login.watching(state["url"]):
            # No password field yet, but a saved login exists: this may be the username page of a two-step login.
            verdict = await self.saved_login.try_login(send, state["url"])
            if verdict:
                return verdict
            if note := self.saved_login.take_note():
                resp = await hitl.ask(self.st, "login", note, url=state["url"])
                return "stop" if resp.get("decision") == "reject" else "reobserve"
        if state.get("login"):
            verdict = await self.saved_login.try_login(send, state["url"])  # the operator's saved login, if any
            if verdict:
                return verdict
            verdict = await self.registration.try_register(send, state["url"])  # a sign-up, if the task allows it
            if verdict:
                return verdict
            message = self.saved_login.take_note() or self.registration.take_note() or (
                f"This page asks for a login. Log in by hand in {browsers.window_name(self.profile)}, "
                "then click “I've handled it”.")
            resp = await hitl.ask(self.st, "login", message, url=state["url"])
            return "stop" if resp.get("decision") == "reject" else "reobserve"
        return "ok"

    async def approve(self, action: dict, state: dict) -> bool:
        return await click_approval.approve_click(self.ctx, action["label"], state["url"], kind=action["kind"])

    async def ask_value(self, action: dict, state: dict) -> str | None:
        resp = await hitl.ask(self.st, "help",
                              f"What should I type into “{action['label']}”? Write it in the note, then click "
                              "“I've handled it”. (Never a password, card number or code — do those by hand.)",
                              url=state["url"])
        if resp.get("decision") == "reject":
            return None
        return (resp.get("note") or "").strip() or None

    async def pick_file(self, action: dict, state: dict) -> str | None:
        resp = await hitl.ask(self.st, "help",
                              f"Which file should I attach to “{action['label']}”? Write the full path on this computer "
                              "in the note, then click “I've handled it”.", url=state["url"])
        if resp.get("decision") == "reject":
            return None
        path = (resp.get("note") or "").strip().strip('"')
        if not path or not Path(path).is_file():
            await self.ctx.log(f"No such file: {path or '(empty)'}", level="warn")
            return None
        return str(Path(path).resolve())

    async def on_step(self, record: dict, state: dict) -> None:
        shot = None
        if state.get("screenshot"):
            path = self.shots_dir / f"{record['step']:03d}.jpg"
            path.write_bytes(base64.b64decode(state["screenshot"]))
            shot = path.relative_to(settings().data_dir).as_posix()
        p = await db.pool()
        await p.execute("update subtasks set steps = $2 where id = $1", self.st["id"], record["step"])
        how = "recorded step" if record["guided"] else ("Jev, in place of a recorded step" if record.get("healed") else "Jev")
        typed = f" ← “{record['text']}”" if record.get("text") else ""
        await self.ctx.log(
            f"{record['kind']} “{record['action']}”{typed} ({how})",
            type="step",
            data={"step": record["step"], "url": state.get("url"), "title": state.get("title"),
                  "actions": [{record["kind"]: {"label": record["action"]}}], "screenshot": shot,
                  "engine": "fast", "guided": record["guided"], "healed": record.get("healed", False),
                  "page_changed": record["page_changed"]},
        )


async def _update_path(ctx: RunContext, out: engine.Outcome) -> None:
    """A replay that drifted paid for the change once: the saved automation step learns the path that worked today (R3),
    so the next run follows it with no model call. Never raises: the step's result stands even if the write fails."""
    st = ctx.subtask
    try:
        changed = await automations.update_trace(ctx.task["automation_id"], st["key"], out.trace)
    except Exception as e:  # noqa: BLE001 - logged, never raised
        log.warning("saved path not updated for step %s: %s", st["id"], e)
        return
    if changed:
        await ctx.log(f"Path updated: {out.healed} of {len(st['guide'])} recorded steps had changed on the page; "
                      "the saved automation now follows the new labels",
                      data={"healed": out.healed, "steps": len(out.trace)})


async def _drive(cdp_url: str, ctx: RunContext, hooks: FleetHooks, decide_model: str, text_model: str,
                 *, guide: list[dict] | None) -> engine.Outcome:
    s, st = settings(), ctx.subtask
    async with CDP(cdp_url) as cdp:
        return await engine.run(
            cdp,
            url=st["start_url"],
            goal=prompts.worker_context(st["instructions"], ctx.dependency_results),
            hooks=hooks,
            usage=ctx.usage,
            decide_model=decide_model,
            text_model=text_model,
            guide=guide,
            allowed_domains=allowed_domains(ctx.task["allowed_domains"], (ctx.profile or {}).get("allowed_domains", [])),
            max_steps=s.max_browser_steps,
            max_seconds=s.human_wait_seconds + 600,  # a step may wait on the operator
        )


async def run(ctx: RunContext) -> ExecResult:
    s, st, profile = settings(), ctx.subtask, ctx.profile
    assert profile is not None, "browser subtask needs a leased profile"
    if not st["start_url"]:
        await ctx.log("No start page for the fast browser — using the full browser agent")
        return await full_browser.run(ctx)

    decide_model = await models.model_for("jev")
    text_model = await models.model_for("smart" if st["model_tier"] == "smart" else "fast")
    await openrouter.api_key()  # fail before opening a browser if there's no key

    # A step with nothing recorded need not start blind: the base offers the path that worked on this site for a goal
    # like this one, as a guide the hands may try and the engine heals when the page has moved on (K3). A saved
    # automation's own path always wins, and only that one is written back when it heals (`_update_path`).
    guide = st.get("guide") or await experience.best_path(
        experience.domain_of(st["start_url"]), st.get("instructions") or "")

    try:
        opened = await browsers.open(profile)
    except browsers.BrowserError as e:
        raise ProfileError(str(e)) from e
    if st.get("guide"):
        following = f" — following a recorded path of {len(guide)} steps"
    elif guide:
        following = f" — trying the path that worked here before ({len(guide)} steps)"
    else:
        following = ""
    await ctx.log(f"Fast browser attached to {opened.kind} profile “{profile['name']}”{following}")

    started = time.monotonic()
    try:
        out = await _drive(opened.cdp_url, ctx, FleetHooks(ctx), decide_model, text_model, guide=guide)
    except CDPError as e:
        out = engine.Outcome("error", f"Browser connection problem: {e}")
    # Whatever happened, the experience base learns from it (RULES.md §7): the fast run is one record, a hand-over to the
    # full agent will write its own.
    await experience.record(ctx, out.status, out.reason, out.trace, time.monotonic() - started)

    if out.status in HAND_OVER:
        if out.acted:
            return ExecResult(
                result=f"The fast browser stopped ({out.reason}) after doing something you approved, so it isn't "
                       "handed to the full agent — that could repeat the action. Check the account.",
                success=False, steps=len(out.steps), trace=out.trace)
        await ctx.log(f"Fast browser {out.status} after {len(out.steps)} steps ({out.reason}) — handing over to the "
                      "full browser agent", level="warn")
        return await full_browser.run(ctx)  # same leased profile

    if s.close_browser_after_subtask:
        await browsers.close(profile)

    if not out.finished:  # rejected or stopped by the operator
        return ExecResult(result=out.reason, success=False, steps=len(out.steps), trace=out.trace)

    # Only a finished replay teaches the automation: never after a hand-over (the full agent's path is its own) or
    # after the operator said no.
    if out.healed and st.get("guide") and ctx.task.get("automation_id"):
        await _update_path(ctx, out)

    final = out.final or {}
    steps = "\n".join(f"{r['step']}. {r['kind']} “{r['action']}”" for r in out.steps) or "(none)"
    answer = await llm.complete(
        model=text_model, system=prompts.FAST_RESULT_SYSTEM, usage=ctx.usage,
        user=(f"{prompts.worker_context(st['instructions'], ctx.dependency_results)}\n\n"
              f"Steps taken:\n{steps}\n\nFinal page: {final.get('url')} — {final.get('title')}\n"
              f"<page>\n{(final.get('full_text') or final.get('text') or '')[:40000]}\n</page>"),
    )
    return ExecResult(result=answer, steps=len(out.steps), trace=out.trace)
