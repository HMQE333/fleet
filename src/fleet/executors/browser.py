"""Browser worker: a browser-use agent driving one leased profile (AdsPower, or a local Chrome profile) over CDP.

The profile keeps its own fingerprint, cookies and proxy; Fleet only attaches to
it (fleet/browsers.py opens it). Captchas, logins and irreversible actions go to the operator (RULES.md §2-4).
"""

import base64
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path

from browser_use import ActionResult, Agent, BrowserProfile, BrowserSession, ChatAnthropic, ChatOpenRouter, Tools
from browser_use.llm.base import BaseChatModel
from browser_use.llm.exceptions import ModelProviderError
from browser_use.llm.messages import ContentPartTextParam, SystemMessage
from browser_use.llm.schema import SchemaOptimizer
from browser_use.llm.views import ChatInvokeCompletion

from fleet import browsers, click_approval, db, experience, guard, hitl, models, openrouter, prompts, providers
from fleet.config import settings
from fleet.executors import ExecResult, RunContext
from fleet.registration import Registration
from fleet.saved_login import SavedLogin, cdp_sender
from fleet.fastbrowser import policy, trace as fast_trace

log = logging.getLogger("fleet.browser")


class ProfileError(RuntimeError):
    """The profile's browser couldn't be opened or attached to."""


def _domain_patterns(task_domains: list[str], profile_domains: list[str]) -> list[str] | None:
    if task_domains and profile_domains:
        domains = [d for d in task_domains if any(d == p or d.endswith("." + p) for p in profile_domains)]
    else:
        domains = task_domains or profile_domains
    if not domains:
        return None
    return sorted({pattern for d in domains for pattern in (d, f"*.{d}")})


# The agent's browser never visits this machine or the local network: Fleet's API, AdsPower's
# Local API (local.adspower.net → 127.0.0.1:50325), routers, metadata endpoints. Raw IPs are refused too.
PRIVATE_HOSTS = ["localhost", "*.localhost", "local.adspower.net", "*.local", "*.internal", "*.lan", "*.home", "*.arpa"]


def make_session(cdp_url: str, allowed_domains: list[str] | None) -> BrowserSession:
    return BrowserSession(
        cdp_url=cdp_url,
        browser_profile=BrowserProfile(
            keep_alive=True,  # never let browser-use close the operator's browser window
            allowed_domains=allowed_domains,
            prohibited_domains=PRIVATE_HOSTS,
            block_ip_addresses=True,
            highlight_elements=False,
        ),
    )


SCHEMA_RULES = "\n\nYour reply must be exactly ONE JSON object that follows this JSON schema. No text before or after it, no code fence:\n"


def _json_object(text: str) -> str:
    """The reply with a code fence or a sentence around the JSON taken off."""
    text = text.strip()
    if text.startswith("```"):
        text = text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    if not text.startswith("{") and "{" in text and "}" in text:
        text = text[text.index("{"): text.rindex("}") + 1]
    return text


@dataclass
class FleetChat(ChatOpenRouter):
    """The agent's replies as JSON asked for in the system prompt, not through response_format.

    Real run: the agent's action schema (~17 kB, every browser action plus ours) is turned by OpenRouter into a constrained-decoding
    grammar for Claude, and every Anthropic route answers 400 "compiled grammar is too large", strict or not. The model follows
    the schema fine when it is only told about it, and the reply is validated here against the same pydantic model."""

    async def ainvoke(self, messages, output_format=None, **kwargs):
        if output_format is None:
            return await super().ainvoke(messages, None, **kwargs)
        rules = SCHEMA_RULES + json.dumps(SchemaOptimizer.create_optimized_json_schema(output_format), separators=(",", ":"))
        patched = list(messages)
        for i, message in enumerate(patched):
            if isinstance(message, SystemMessage):
                content = message.content + rules if isinstance(message.content, str) else [*message.content, ContentPartTextParam(text=rules)]
                patched[i] = SystemMessage(content=content)
                break
        else:
            patched.insert(0, SystemMessage(content=rules.strip()))
        reply = await super().ainvoke(patched, None, **kwargs)
        try:
            parsed = output_format.model_validate_json(_json_object(reply.completion))
        except ValueError as e:  # the agent counts it as a failed step and asks again
            raise ModelProviderError(message=f"The reply didn't match the agent's format: {str(e)[:300]}", status_code=502, model=self.name) from e
        return ChatInvokeCompletion(completion=parsed, usage=reply.usage)


def _chat(model: str, api_key: str) -> BaseChatModel:
    """The agent's model client: Anthropic's own API for an `anthropic-api/…` model, else OpenRouter.

    On the direct route the action schema goes through Anthropic's tool use, which takes it as it is, so
    there is nothing to work around and browser-use's own client will do. `max_tokens` is well above what
    an action needs because the newer Claude models think before they answer and that shares the budget.
    """
    if providers.is_direct(model):
        return ChatAnthropic(model=providers.bare(model), api_key=api_key, max_tokens=16000)
    return FleetChat(model=model, api_key=api_key, default_headers={"X-Title": "Fleet"})


def _node_label(node) -> str:
    """What a person sees on the element: its text, else the usual attribute fallbacks."""
    attrs = node.attributes or {}
    text = " ".join((node.get_all_children_text() or "").split())
    return (text or attrs.get("aria-label") or attrs.get("value") or attrs.get("title") or attrs.get("alt") or "").strip()[:160]


class FleetTools(Tools):
    """browser-use's tools, with every click that reads like a commitment ("Submit order", "Send", "Delete"...) sent through the
    operator's approval first. Real run: asked to place an order, the model clicked "Submit order" without calling its own
    request_approval tool, so the rule can't depend on the model remembering it."""

    approve_click = None  # async (label, url) -> bool, set by _build_tools
    pre_approved = False  # the operator just said yes to something the model asked about itself: good for the very next action only

    async def act(self, action, browser_session, *args, **kwargs):
        allowed, self.pre_approved = self.pre_approved, False
        click = action.model_dump(exclude_unset=True).get("click")
        if self.approve_click and click and (click.get("index") or click.get("coordinate_x") is not None):
            if click.get("index"):
                node = await browser_session.get_element_by_index(click["index"])
            else:  # a click by position: the element under it is what gets pressed
                node = await browser_session.get_dom_element_at_coordinates(click["coordinate_x"], click.get("coordinate_y") or 0)
            label = _node_label(node) if node is not None else ""
            if label and policy.needs_approval("click", label) and not allowed:
                url = await browser_session.get_current_page_url()
                if not await self.approve_click(label, url):
                    return ActionResult(extracted_content=f"The operator did NOT approve clicking “{label}”. Do not click it or anything "
                                                          "that does the same; report that it wasn't approved.", include_in_memory=True)
        return await super().act(action, browser_session, *args, **kwargs)


def _build_tools(ctx: RunContext) -> Tools:
    tools = FleetTools()
    st = ctx.subtask

    async def approve_click(label: str, url: str) -> bool:
        return await click_approval.approve_click(ctx, label, url)

    tools.approve_click = approve_click

    @tools.action(
        "Ask the human operator to take over something you must not do yourself: a captcha or "
        "'verify you are human' page, a 2FA prompt, a login form, or anything unexpected that blocks you. "
        "Describe what you see. Waits until the operator is done."
    )
    async def request_human(reason: str, browser_session: BrowserSession) -> ActionResult:
        url = await browser_session.get_current_page_url()
        resp = await hitl.ask(st, "help", reason, url=url)
        note = resp.get("note") or ""
        if resp.get("decision") == "reject":
            return ActionResult(is_done=True, success=False, extracted_content=f"The operator stopped this subtask. {note}")
        return ActionResult(
            extracted_content=f"The operator handled it. {note} Look at the page again and continue.".strip(),
            include_in_memory=True,
        )

    @tools.action(
        "Ask the operator for approval BEFORE an irreversible action: payment or purchase, sending a message or "
        "email to someone, publishing or posting, deleting data, accepting terms, changing account settings. "
        "Describe exactly what you will do. Set is_payment true for payments and purchases."
    )
    async def request_approval(action: str, is_payment: bool, browser_session: BrowserSession) -> ActionResult:
        if not ctx.task["require_approval"] and not is_payment:
            await ctx.log(f"Approval is off for this task — proceeding: {action}")
            await click_approval.mark_side_effects(st)
            return ActionResult(extracted_content="Approved (approval is turned off for this task).", include_in_memory=True)
        url = await browser_session.get_current_page_url()
        resp = await hitl.ask(st, "approval", action, url=url, payment=is_payment)
        note = resp.get("note") or ""
        if resp.get("decision") == "approve":
            await click_approval.mark_side_effects(st)
            tools.pre_approved = True
            return ActionResult(extracted_content=f"APPROVED by the operator. {note}".strip(), include_in_memory=True)
        return ActionResult(
            extracted_content=f"REJECTED by the operator — do not do it. {note}".strip(), include_in_memory=True
        )

    return tools


async def run(ctx: RunContext) -> ExecResult:
    s = settings()
    st, profile = ctx.subtask, ctx.profile
    assert profile is not None, "browser subtask needs a leased profile"
    model = await models.model_for("browser")
    # fail before opening a browser if the key this model needs isn't there
    llm_key = await (providers.anthropic_key() if providers.is_direct(model) else openrouter.api_key())

    try:
        opened = await browsers.open(profile)
    except browsers.BrowserError as e:
        raise ProfileError(str(e)) from e
    await ctx.log(f"Attached to {opened.kind} profile “{profile['name']}”")

    shots_dir = s.screenshots_dir / str(st["id"])
    shots_dir.mkdir(parents=True, exist_ok=True)
    pool = await db.pool()

    async def on_step(state, model_output, n_steps: int) -> None:
        shot = None
        if state.screenshot:
            path = shots_dir / f"{n_steps:03d}.png"
            path.write_bytes(base64.b64decode(state.screenshot))
            shot = path.relative_to(s.data_dir).as_posix()
        actions = [a.model_dump(exclude_none=True, exclude_unset=True) for a in (model_output.action or [])]
        await pool.execute("update subtasks set steps = $2 where id = $1", st["id"], n_steps)
        await ctx.log(
            model_output.next_goal or model_output.memory or f"Step {n_steps}",
            type="step",
            data={"step": n_steps, "url": state.url, "title": state.title, "actions": actions, "screenshot": shot},
        )

    async def should_stop() -> bool:
        status = await pool.fetchval("select status from subtasks where id = $1", st["id"])
        return status == "cancelled"

    saved_login = SavedLogin(ctx)
    registration = Registration(ctx)

    async def use_saved_login(agent: Agent) -> None:
        """Between steps: if the page is a login form and the operator saved a login for it, use it (their way)."""
        try:
            cdp = await agent.browser_session.get_or_create_cdp_session(focus=False)
            url = await agent.browser_session.get_current_page_url()
            send = cdp_sender(cdp)
            verdict = await saved_login.try_login(send, url) or await registration.try_register(send, url)
            if verdict is None and (note := registration.take_note()):  # Fleet won't sign up here: the operator decides
                resp = await hitl.ask(st, "login", note, url=url)
                verdict = "stop" if resp.get("decision") == "reject" else "reobserve"
        except (hitl.HumanTimeout, hitl.Cancelled):
            raise
        except Exception as e:  # a login problem must never break the agent's own step
            log.warning("saved login skipped: %s", e)
            return
        if verdict == "stop":
            agent.stop()

    async def on_step_end(agent: Agent) -> None:
        if await hitl.take_over_requested(st["id"]):
            await ctx.log("You took over this step: paused until you give control back", level="warn")
            url = await agent.browser_session.get_current_page_url()
            resp = await hitl.ask(st, "takeover", hitl.take_over_message(profile["name"]), url=url)
            if resp.get("decision") == "reject":
                agent.stop()
                return
        reason = await guard.detect_challenge(agent.browser_session)
        if not reason:
            await use_saved_login(agent)
            return
        url = await agent.browser_session.get_current_page_url()
        await ctx.log(f"{reason} detected — pausing for the operator", level="warn")
        resp = await hitl.ask(
            st,
            "challenge",
            f"{reason} on the page. Please complete it in {browsers.window_name(profile)}, then click “I've handled it”.",
            url=url,
        )
        if resp.get("decision") == "reject":
            agent.stop()

    task_text = prompts.worker_context(st["instructions"], ctx.dependency_results)
    if st["start_url"]:
        task_text += f"\n\nStart at: {st['start_url']}"

    session = make_session(opened.cdp_url, _domain_patterns(ctx.task["allowed_domains"], profile["allowed_domains"]))
    agent = Agent(
        task=task_text,
        llm=_chat(model, llm_key),
        browser_session=session,
        tools=_build_tools(ctx),
        extend_system_message=prompts.browser_rules(bool(ctx.task.get("allow_registration"))),
        register_new_step_callback=on_step,
        register_should_stop_callback=should_stop,
        use_vision=True,
        use_judge=False,
        max_failures=3,
        step_timeout=s.human_wait_seconds + 300,  # a step may be waiting on the operator
        enable_signal_handler=False,
    )

    history_path = s.histories_dir / f"{st['id']}.json"
    started = time.monotonic()
    try:
        history = await agent.run(max_steps=s.max_browser_steps, on_step_end=on_step_end)
    finally:
        try:
            agent.save_history(history_path)
        except Exception as e:
            log.warning("could not save history: %s", e)
        await agent.close()
        if s.close_browser_after_subtask:
            await browsers.close(profile)

    if history.usage:
        await ctx.usage.add_tokens(model, history.usage.total_prompt_tokens, history.usage.total_completion_tokens)

    try:
        trace = fast_trace.from_browser_use(history)
    except Exception as e:  # a recording problem must never fail the step itself
        log.warning("could not record a replayable path: %s", e)
        trace = None

    final = history.final_result()
    done = history.is_done()
    success = bool(done and history.is_successful() is not False)
    if not final:
        errors = [e for e in history.errors() if e]
        final = "Stopped before finishing." + (f" Last error: {errors[-1]}" if errors else "")
    # The experience base learns from every run (RULES.md §7): the outcome and the path, never the agent's prose.
    await experience.record(ctx, "done" if success else "failed",
                            "" if success else ("finished without success" if done else "stopped before finishing"),
                            trace, time.monotonic() - started)
    return ExecResult(result=final, success=success, steps=history.number_of_steps(),
                      history_path=str(Path(history_path)), trace=trace)
