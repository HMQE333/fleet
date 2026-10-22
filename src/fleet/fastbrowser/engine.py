"""The fast browser loop: observe → (follow recorded step | ask Jev) → safety gates → act → record.

Structure from jev-ultrafast's agent.py (decision consumed once, freshness before input, record the
action before observing), stop gates and next-best recovery from JevBrowser (MIT, see NOTICE.md).
Everything Fleet-specific (asking the operator, approvals, screenshots, cancel) goes through Hooks,
so the engine itself has no database or UI code and can be tested against a real browser.
"""

import asyncio
import logging
import re
from dataclasses import dataclass, field

from fleet import llm
from fleet.fastbrowser import decide, netguard, policy
from fleet.fastbrowser.cdp import CDP, CDPError
from fleet.fastbrowser.page import FULL_TEXT_JS, Page, StalePage

log = logging.getLogger("fleet.fastbrowser")

GOAL_DONE_THRESHOLD = 0.85
STUCK_THRESHOLD = 0.85
MAX_BLIND_SCROLLS = 4  # screens read in a row when Jev sees nothing useful, before the step is handed over
MAX_BACK_OUTS = 2  # dead ends the engine may walk back out of before it hands the step over


class Hooks:
    """Fleet's side of the loop. Defaults are the safe choice (e.g. never approve)."""

    async def should_stop(self) -> bool:
        return False

    async def on_page(self, state: dict, page: Page) -> str:
        """'ok', 'reobserve' (a human changed the page), or 'stop'."""
        return "ok"

    async def approve(self, action: dict, state: dict) -> bool:
        return False

    async def ask_value(self, action: dict, state: dict) -> str | None:
        return None

    async def pick_file(self, action: dict, state: dict) -> str | None:
        """The local file to attach to a file input, or None (the operator chooses; never the page or the model)."""
        return None

    async def on_step(self, record: dict, state: dict) -> None:
        return None


@dataclass
class Outcome:
    status: str  # done | goal_achieved | stuck | blocked | rejected | stopped | max_steps | timeout | error
    reason: str = ""
    steps: list[dict] = field(default_factory=list)
    final: dict | None = None  # url, title, text of the last page
    acted: bool = False  # an approved irreversible action was executed
    trace: list[dict] = field(default_factory=list)  # replayable steps: kind, label, value
    healed: int = 0  # recorded steps that no longer fit the page and were replaced by a Jev decision (the trace has the new labels)

    @property
    def finished(self) -> bool:
        return self.status in ("done", "goal_achieved")


def _norm(label: str | None) -> str:
    return re.sub(r"\s+", " ", label or "").strip().casefold()


def match_recorded(step: dict, state: dict) -> dict | None:
    """The one observed action that is exactly the recorded step, or None (then Jev decides)."""
    if step.get("kind") == "back":
        return decide.BACK
    if step.get("kind") == "scroll":
        wanted = "scroll_up" if (step.get("label") or "").lower().endswith("up") else "scroll_down"
        return next((a for a in state["actions"] if a["id"] == wanted), None)
    candidates = [
        a for a in decide.filter_actions(state["actions"])
        if a["kind"] == step.get("kind") and _norm(a["label"]) == _norm(step.get("label"))
    ]
    return candidates[0] if len(candidates) == 1 else None


def replaces(step: dict | None, action: dict) -> bool:
    """Jev's action stands in for the recorded step: the same kind of action under another label (the site renamed the
    button or the field). A different kind is an extra step on the way (a banner to close), not a replacement, and
    the same label is the recorded step itself."""
    if not step or step.get("kind") in ("back", "scroll"):
        return False
    return action["kind"] == step.get("kind") and _norm(action["label"]) != _norm(step.get("label"))


def describe(step: dict) -> str:
    text = f'{step.get("kind")} “{step.get("label")}”'
    return text + (f' with “{step["value"]}”' if step.get("value") else "")


async def run(
    cdp: CDP,
    *,
    url: str,
    goal: str,
    hooks: Hooks,
    usage: llm.Usage,
    decide_model: str,
    text_model: str,
    guide: list[dict] | None = None,
    allowed_domains: list[str] | None = None,
    max_steps: int = 40,
    max_seconds: float = 300,
    allow_private: bool = False,  # tests only: lets the engine drive pages served from 127.0.0.1
) -> Outcome:
    guide = guide or []
    allowed = allowed_domains or []
    if not allow_private and (reason := await netguard.check_url_resolved(url)):
        return Outcome("blocked", f"Start page refused: {reason}")
    if not netguard.host_allowed(url, allowed):
        return Outcome("blocked", f"Start page is outside the allowed domains ({', '.join(allowed)})")

    loop = asyncio.get_running_loop()
    started = loop.time()
    out = Outcome("error")
    history: list[dict] = []
    guide_i = 0
    approved: set[tuple] = set()
    no_change = domain_strikes = blind_scrolls = back_outs = 0
    depth = 0  # pages the engine navigated into itself: how far back it may still step without leaving the run
    last_choice = None
    tabs: list[Page] = []  # the tabs behind the current one: a click opened a new tab and the engine followed it
    try:
        page = await Page.open(cdp, url)
    except (CDPError, StalePage) as e:
        return Outcome("error", f"Couldn't open the start page: {e}")

    async def go_back() -> None:
        """In a tab the engine followed into: close it and return to the tab that opened it; else browser history."""
        nonlocal page, depth
        depth = max(0, depth - 1)
        if tabs:
            await page.close()
            page = tabs.pop()
        else:
            await page.back()

    try:
        state = await page.observe()
        while True:
            out.final = {k: state.get(k) for k in ("url", "title", "text")}
            if len(history) >= max_steps:
                out.status, out.reason = "max_steps", f"Reached {max_steps} steps"
                break
            if loop.time() - started > max_seconds:
                out.status, out.reason = "timeout", f"Took longer than {max_seconds:.0f}s"
                break
            if await hooks.should_stop():
                out.status, out.reason = "stopped", "Cancelled"
                break

            current = state["url"]
            checked = not current.startswith("about:") and not allow_private
            if checked and (reason := await netguard.check_url_resolved(current)):
                out.status, out.reason = "blocked", f"Stopped: the page went to {current} ({reason})"
                break
            if not netguard.host_allowed(current, allowed):
                domain_strikes += 1
                if domain_strikes >= 2:
                    out.status, out.reason = "blocked", f"Kept leaving the allowed domains ({current})"
                    break
                await go_back()
                state = await page.observe()
                continue

            verdict = await hooks.on_page(state, page)
            if verdict == "stop":
                out.status, out.reason = "stopped", "The operator stopped this step"
                break
            if verdict == "reobserve":
                state = await page.observe()
                continue

            # 1) Recorded path: act without a model call when the page shows exactly that element.
            step_hint = guide[guide_i] if guide_i < len(guide) else None
            action = match_recorded(step_hint, state) if step_hint else None
            decision: dict = {"operation": "RECORDED", "guided": True} if action else {}

            # 2) Otherwise Jev decides (with the recorded step as a hint).
            if action is None:
                decision = await decide.choose(state, goal, history, model=decide_model, usage=usage,
                                               hint=describe(step_hint) if step_hint else None)
                choice = decision["choice"]
                if choice == "DONE":
                    if await page.fresh(state):
                        out.status, out.reason = "done", "Jev confirmed the goal is met"
                        break
                    state = await page.observe()
                    continue
                if choice == "BLOCKED":
                    # Jev is only ever shown what the viewport holds, so "nothing here helps" is not an answer about
                    # the page until the page has been seen: while there is more below, read on instead of handing the
                    # step over (a "Next" link under ten quotes is the ordinary case).
                    down = next((a for a in state["actions"] if a["id"] == "scroll_down"), None)
                    if down is not None and blind_scrolls < MAX_BLIND_SCROLLS:
                        blind_scrolls += 1
                        choice, decision["operation"] = down["id"], "SCROLL_DOWN"
                    elif back_outs < MAX_BACK_OUTS and depth > 0:
                        # A dead end the engine walked into itself, with the whole page read: the page it came from
                        # may still hold the way on (the pager under a list it left by clicking into one item), so
                        # step back and read the rest of that one. Code decides this, not the model: Jev is asked
                        # about the current page only, and "back" is the one answer it can't see the value of.
                        back_outs, blind_scrolls = back_outs + 1, 0
                        choice, decision["operation"] = "back", "BACK_OUT"
                    else:
                        out.status, out.reason = "blocked", "Jev found no supported action that makes progress"
                        break
                else:
                    blind_scrolls = 0
                if decision["goal_done"] > GOAL_DONE_THRESHOLD:
                    out.status, out.reason = "goal_achieved", "The goal watcher says the page shows the result"
                    break
                if decision["stuck"] > STUCK_THRESHOLD and len(history) > 2:
                    out.status, out.reason = "stuck", "The stuck watcher fired"
                    break
                if choice == last_choice and history and history[-1]["page_changed"] is False:
                    alternate = decide.pick_alternate(decision["probabilities"], {choice})
                    if alternate:
                        choice, decision["alternate"] = alternate, True
                action = decide.BACK if choice == "back" else next(
                    (a for a in state["actions"] if a["id"] == choice), None)
                if action is None:
                    state = await page.observe()
                    continue

            # A recorded step that no longer fits (its label changed) is healed: Jev's same-kind action takes its place,
            # the path moves on, and the new label lands in the trace so the saved path can be updated once.
            healed = not decision.get("guided") and replaces(step_hint, action)

            # 3) Safety gates (code, not AI): irreversible clicks need the operator.
            key = (action.get("node"), action["kind"], _norm(action["label"]))
            if policy.needs_approval(action["kind"], action["label"]) and key not in approved:
                if not await hooks.approve(action, state):
                    out.status, out.reason = "rejected", f"The operator didn't approve “{action['label']}”"
                    break
                approved.add(key)
                out.acted = True

            text = None
            if action["kind"] == "fill":
                # A recorded or healed fill types what was recorded: one renamed field costs one decision, not a text call too.
                text = (step_hint or {}).get("value") if decision.get("guided") or healed else None
                if not text:
                    text = await decide.field_text(goal, action, state, history, model=text_model, usage=usage)
                if not text:
                    text = await hooks.ask_value(action, state)
                if not text:
                    out.status, out.reason = "blocked", f"Nothing to type into “{action['label']}”"
                    break
            elif action["kind"] == "upload":
                # The path comes from the operator alone: never from the page, the model or a recorded step.
                text = await hooks.pick_file(action, state)
                if not text:
                    out.status, out.reason = "blocked", f"No file was chosen for “{action['label']}”"
                    break

            # 4) Act. A stale page means: observe again and decide again; nothing was executed.
            try:
                if action["kind"] == "back":
                    await go_back()
                else:
                    await page.act(action, state, text=text)
            except StalePage:
                state = await page.observe()
                continue

            # A click that opened a new tab: the engine follows it (the address is checked like any page at the top of
            # the loop) and keeps its own tab behind it for "back".
            opened = await page.opened_tab()
            new_state = None if opened else await page.observe()
            if opened is None:  # the tab may have been reported while the page was being read
                opened = await page.opened_tab()
            if opened is not None:
                tabs.append(page)
                page = opened
                new_state = await page.observe()
            changed = new_state["fingerprint"] != state["fingerprint"]
            if new_state["url"] != state["url"] and action["kind"] != "back":
                depth += 1
            record = {
                "step": len(history) + 1,
                "action": action["label"],
                "kind": action["kind"],
                "text": text,
                "url": state["url"],
                "page_changed": changed,
                "guided": bool(decision.get("guided")),
                "healed": healed,
                "operation": decision.get("operation"),
                "confidence": decision.get("confidence"),
                "latency_ms": decision.get("latency_ms", 0),
            }
            history.append(record)
            out.steps.append(record)
            out.trace.append({"kind": action["kind"], "label": action["label"], "value": text})
            if step_hint and (decision.get("guided") or healed or (
                    action["kind"] == step_hint.get("kind") and _norm(action["label"]) == _norm(step_hint.get("label")))):
                guide_i += 1
            out.healed += healed
            await hooks.on_step(record, new_state)

            last_choice = action.get("id")
            no_change = no_change + 1 if not changed and action["kind"] != "wait" else 0
            state = new_state
            if no_change >= 3:
                out.status, out.reason = "stuck", "Three actions in a row changed nothing"
                break
    except (CDPError, StalePage) as e:
        out.status, out.reason = "error", f"Browser problem: {e}"
    except llm.LLMError as e:
        out.status, out.reason = "error", str(e)
    finally:
        if out.final is not None:
            try:  # the reader only sees the viewport; the result needs the whole page
                out.final["full_text"] = await page.evaluate(FULL_TEXT_JS)
            except (CDPError, StalePage):
                pass
        for tab in (page, *reversed(tabs)):  # the tabs the engine followed into, then its own
            await tab.close()
    return out
