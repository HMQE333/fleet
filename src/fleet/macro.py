"""Macros: a step list the brain writes once and the hands run with no model in the loop (READINESS.md, gate B2).

A macro is plain data (navigate, click, fill, select, wait_for, extract, back, scroll, assert_text), validated before
anything runs, executed through the fast browser's page reader and hit-tested inputs (fleet/fastbrowser/page.py), and
held to the same rules as every other browser step: labels must be on the page, a click that reads like a commitment
waits for the operator (policy.needs_approval → hooks.approve), private addresses are refused, and no step can type into
a password, card or one-time-code field (the page reader never offers those; validate() refuses the labels up front too).
The trace of a finished macro is the same replayable path the engine records, so a macro that worked becomes an
automation like any other browser step.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, Field, ValidationError, model_validator

from fleet import llm
from fleet.fastbrowser import decide, engine, netguard, policy
from fleet.fastbrowser.cdp import CDP, CDPError
from fleet.fastbrowser.page import Page, StalePage

KINDS = ("navigate", "click", "fill", "select", "wait_for", "extract", "back", "scroll", "assert_text")
MAX_STEPS = 60

# A macro may never carry a secret: these labels are refused before a browser is even opened (RULES.md §3).
SECRET_LABEL = re.compile(
    r"passw|hasło|haslo|\bpin\b|cvv|cvc|card ?number|numer karty|one[- ]time|\botp\b|kod (sms|jednorazowy)|2fa|security code",
    re.IGNORECASE,
)


class MacroError(ValueError):
    """The macro is not something Fleet will run. The message says why."""


class Step(BaseModel):
    kind: Literal["navigate", "click", "fill", "select", "wait_for", "extract", "back", "scroll", "assert_text"]
    url: str | None = None  # navigate
    label: str | None = None  # click, fill, select: the element's visible name
    value: str | None = None  # fill: what to type; select: the option's label or value
    text: str | None = None  # wait_for, assert_text: text that must be visible
    seconds: float = Field(default=10, ge=0, le=120)  # wait_for
    name: str | None = None  # extract: key in the result
    what: str | None = None  # extract: "text" (default, the page's visible text), "url" or "title"
    direction: Literal["down", "up"] = "down"  # scroll

    @model_validator(mode="after")
    def _complete(self) -> Step:
        need = {"navigate": ("url",), "click": ("label",), "fill": ("label", "value"), "select": ("label", "value"),
                "wait_for": ("text",), "extract": ("name",), "back": (), "scroll": (), "assert_text": ("text",)}
        for attr in need[self.kind]:
            if not (getattr(self, attr) or "").strip():
                raise ValueError(f"{self.kind} needs {attr}")
        if self.kind == "fill" and SECRET_LABEL.search(self.label or ""):
            raise ValueError(f"a macro never types into “{self.label}”: passwords, card numbers and codes are the operator's (RULES.md §3)")
        if self.kind == "navigate" and not re.match(r"^https?://", self.url or ""):
            raise ValueError("navigate needs an http(s) URL")
        return self


class Macro(BaseModel):
    name: str = ""
    steps: list[Step] = Field(min_length=1, max_length=MAX_STEPS)


def validate(data: dict | Macro) -> Macro:
    """A Macro from plain data, or MacroError saying which step is wrong and why."""
    if isinstance(data, Macro):
        return data
    try:
        return Macro.model_validate(data)
    except ValidationError as e:
        problems = []
        for err in e.errors()[:5]:
            loc = ".".join(str(p) for p in err["loc"]) or "macro"
            problems.append(f"{loc}: {err['msg']}")
        raise MacroError("; ".join(problems)) from e


@dataclass
class MacroOutcome(engine.Outcome):
    extracted: dict[str, str] = field(default_factory=dict)


def _norm(label: str | None) -> str:
    return re.sub(r"\s+", " ", label or "").strip().casefold()


def find_action(state: dict, kind: str, label: str, value: str | None = None) -> dict | None:
    """The one observed action for a step: exact visible name first, then a prefix ("Next page" fits "Next page »").
    A select step matches the option by its label or value."""
    wanted = _norm(label)
    candidates = [a for a in decide.filter_actions(state["actions"]) if a["kind"] == kind]
    if kind == "select":
        option = _norm(value)
        candidates = [a for a in candidates if _norm(a["label"].split(" → ")[0]) == wanted
                      and (_norm(a["label"].split(" → ")[-1]) == option or _norm(a.get("value")) == option)]
        return candidates[0] if len(candidates) == 1 else None
    exact = [a for a in candidates if _norm(a["label"]) == wanted]
    if len(exact) == 1:
        return exact[0]
    prefix = [a for a in candidates if _norm(a["label"]).startswith(wanted)]
    return prefix[0] if len(prefix) == 1 else None


def describe(step: Step) -> str:
    bits = [step.kind]
    for attr in ("url", "label", "value", "text", "name", "direction"):
        v = getattr(step, attr)
        if v:
            bits.append(f"“{v}”")
    return " ".join(bits)


async def run(
    cdp: CDP,
    macro: Macro | dict,
    *,
    hooks: engine.Hooks | None = None,
    usage: llm.Usage | None = None,
    allowed_domains: list[str] | None = None,
    allow_private: bool = False,  # tests only: pages served from 127.0.0.1
    max_seconds: float = 300,
) -> MacroOutcome:
    """Run the macro's steps in order with no model call. Stops at the first step that can't be done, honestly:
    done | rejected (the operator said no) | blocked (private address, missing element) | stuck (wait_for / assert_text
    not met) | stopped | timeout | error. `usage` is only passed through so callers can assert nothing was spent."""
    macro = validate(macro)
    hooks = hooks or engine.Hooks()
    allowed = allowed_domains or []
    out = MacroOutcome("error")
    loop = asyncio.get_running_loop()
    started = loop.time()
    page: Page | None = None
    state: dict | None = None
    approved: set[tuple] = set()

    async def guard_url(url: str) -> str | None:
        if not allow_private and (reason := await netguard.check_url_resolved(url)):
            return f"{url} refused ({reason})"
        if not netguard.host_allowed(url, allowed):
            return f"{url} is outside the allowed domains ({', '.join(allowed)})"
        return None

    async def observe() -> dict:
        assert page is not None
        return await page.observe(screenshot=False)

    async def finish(status: str, reason: str) -> MacroOutcome:
        out.status, out.reason = status, reason
        if page is not None:
            try:
                final_state = state or await observe()
                out.final = {k: final_state.get(k) for k in ("url", "title", "text")}
                out.final["full_text"] = await page.evaluate("document.body ? document.body.innerText.slice(0, 40000) : ''")
            except (CDPError, StalePage):
                pass
            await page.close()
        return out

    try:
        for n, step in enumerate(macro.steps, start=1):
            if loop.time() - started > max_seconds:
                return await finish("timeout", f"Took longer than {max_seconds:.0f}s at step {n} ({describe(step)})")
            if await hooks.should_stop():
                return await finish("stopped", "Cancelled")

            if step.kind == "navigate":
                if reason := await guard_url(step.url):
                    return await finish("blocked", reason)
                if page is None:
                    page = await Page.open(cdp, step.url)
                else:
                    await page.call("Page.navigate", url=step.url)
                    await page.wait_ready(20)
                state = await observe()
                out.steps.append({"step": n, "action": step.url, "kind": "navigate", "url": state["url"]})
                continue
            if page is None:
                return await finish("error", f"Step {n} ({describe(step)}) comes before any navigate")
            state = state or await observe()
            if reason := await guard_url(state["url"]):
                return await finish("blocked", f"Stopped: the page went to {reason}")
            verdict = await hooks.on_page(state, page)
            if verdict == "stop":
                return await finish("stopped", "The operator stopped this step")
            if verdict == "reobserve":
                state = await observe()

            if step.kind == "wait_for":
                deadline = loop.time() + step.seconds
                while step.text not in (state.get("text") or "") and step.text not in await page.evaluate(
                        "document.body ? document.body.innerText.slice(0, 40000) : ''"):
                    if loop.time() > deadline:
                        return await finish("stuck", f"Step {n}: “{step.text}” didn't appear within {step.seconds:.0f}s")
                    await asyncio.sleep(0.25)
                    state = await observe()
                out.steps.append({"step": n, "action": step.text, "kind": "wait_for", "url": state["url"]})
                continue
            if step.kind == "assert_text":
                full = await page.evaluate("document.body ? document.body.innerText.slice(0, 40000) : ''")
                if step.text not in (state.get("text") or "") and step.text not in (full or ""):
                    return await finish("stuck", f"Step {n}: the page doesn't show “{step.text}”")
                out.steps.append({"step": n, "action": step.text, "kind": "assert_text", "url": state["url"]})
                continue
            if step.kind == "extract":
                what = (step.what or "text").lower()
                if what == "url":
                    out.extracted[step.name] = state["url"]
                elif what == "title":
                    out.extracted[step.name] = state.get("title") or ""
                else:
                    out.extracted[step.name] = (await page.evaluate("document.body ? document.body.innerText.slice(0, 40000) : ''")) or ""
                out.steps.append({"step": n, "action": step.name, "kind": "extract", "url": state["url"]})
                continue
            if step.kind == "back":
                await page.back()
                state = await observe()
                out.steps.append({"step": n, "action": "back", "kind": "back", "url": state["url"]})
                out.trace.append({"kind": "back", "label": "back", "value": None})
                continue
            if step.kind == "scroll":
                wanted = "scroll_up" if step.direction == "up" else "scroll_down"
                action = next((a for a in state["actions"] if a["id"] == wanted), None)
                if action is not None:
                    try:
                        await page.act(action, state)
                    except StalePage:
                        state = await observe()
                        continue
                state = await observe()
                out.steps.append({"step": n, "action": wanted, "kind": "scroll", "url": state["url"]})
                out.trace.append({"kind": "scroll", "label": "Scroll up" if step.direction == "up" else "Scroll down", "value": None})
                continue

            # click / fill / select: the element must be on the page right now, and a commitment waits for the operator.
            action = None
            for _attempt in range(2):
                action = find_action(state, step.kind, step.label, step.value)
                if action is not None:
                    break
                await asyncio.sleep(0.5)
                state = await observe()
            if action is None:
                offered = ", ".join(sorted({a["label"] for a in decide.filter_actions(state["actions"]) if a["kind"] == step.kind})[:12])
                return await finish("blocked", f"Step {n}: no {step.kind} “{step.label}” on {state['url']} (offered: {offered})")
            key = (action.get("node"), action["kind"], _norm(action["label"]))
            if policy.needs_approval(action["kind"], action["label"]) and key not in approved:
                if not await hooks.approve(action, state):
                    return await finish("rejected", f"The operator didn't approve “{action['label']}”")
                approved.add(key)
                out.acted = True
            try:
                await page.act(action, state, text=step.value if step.kind == "fill" else None)
            except StalePage:
                state = await observe()
                action = find_action(state, step.kind, step.label, step.value)
                if action is None:
                    return await finish("blocked", f"Step {n}: “{step.label}” disappeared while the page changed")
                await page.act(action, state, text=step.value if step.kind == "fill" else None)
            new_state = await observe()
            out.steps.append({"step": n, "action": action["label"], "kind": action["kind"], "text": step.value if step.kind == "fill" else None,
                              "url": state["url"], "page_changed": new_state["fingerprint"] != state["fingerprint"], "guided": True,
                              "operation": "MACRO"})
            out.trace.append({"kind": action["kind"], "label": action["label"], "value": step.value if step.kind == "fill" else None})
            await hooks.on_step(out.steps[-1], new_state)
            state = new_state
        return await finish("done", f"All {len(macro.steps)} steps done")
    except (CDPError, StalePage) as e:
        return await finish("error", f"Browser problem: {e}")
