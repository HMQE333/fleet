"""The operator's OK for a click that reads like a commitment (RULES.md §4) — one implementation for both browser workers.

The fast engine asks before it clicks. The full agent used to rely on the model calling its own `request_approval` tool, and a real
run showed it just clicking "Submit order" without asking, so its clicks now pass through here too. Code decides, not the model.
"""

from fleet import db, hitl, trust
from fleet.executors import RunContext
from fleet.fastbrowser import policy


async def mark_side_effects(subtask: dict) -> None:
    """From here on the subtask must not be retried automatically (it would repeat the action)."""
    p = await db.pool()
    await p.execute("update subtasks set side_effects = true where id = $1", subtask["id"])


async def approve_click(ctx: RunContext, label: str, url: str, *, kind: str = "click") -> bool:
    """True when the click may go ahead: approval is off for this task (never for payments), a trust rule covers it, or the operator
    said yes in the Inbox. Marks the step as having side effects so it is never retried on its own."""
    st, profile = ctx.subtask, ctx.profile or {}
    payment = policy.is_payment(label)
    if not ctx.task["require_approval"] and not payment:
        await ctx.log(f"Approval is off for this task — clicking “{label}”")
        await mark_side_effects(st)
        return True
    profile_id = profile.get("id")
    if kind == "click" and ctx.task.get("use_trust_rules", True) and (rule := await trust.find(url, label, profile_id)):
        await trust.used(rule["id"])
        await ctx.log(f"Approved by your trust rule — clicking “{label}” on {rule['domain']} (Settings → Trust rules)", type="trust")
        await mark_side_effects(st)
        return True
    offer = trust.offer(url, label) if kind == "click" and profile_id else None
    resp = await hitl.ask(st, "approval", f"Click “{label}” on {url}" + (" — this looks like a payment." if payment else "."),
                          url=url, offer=offer, payment=payment)
    if resp.get("decision") != "approve":
        return False
    await mark_side_effects(st)
    if offer and resp.get("remember"):
        await trust.add(profile_id, offer["domain"], offer["label"])
        await ctx.log(f"Rule saved: always allow “{offer['label']}” on {offer['domain']} for profile “{profile.get('name')}”. "
                      "Remove it any time in Settings → Trust rules.", type="trust")
    return True
