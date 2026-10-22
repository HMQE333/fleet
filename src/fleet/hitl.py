"""Human-in-the-loop: pause a subtask, ask the operator, wait for their answer."""

import asyncio
import time
from datetime import UTC, datetime
from uuid import UUID

from fleet import db, events
from fleet.config import settings


class HumanTimeout(RuntimeError):
    pass


class Cancelled(RuntimeError):
    pass


async def ask(subtask: dict, kind: str, message: str, *, url: str | None = None, offer: dict | None = None,
              payment: bool | None = None) -> dict:
    """Block until the operator answers in the panel.

    kind: "challenge" (captcha / verification / 2FA), "login", "login_saved", "register", "verify_email",
    "approval", or "help".
    `offer` (approval cards only): {"domain", "label"} the operator may answer "approve and always allow" to
    (see fleet/trust.py). `payment` (approval cards only): whether the action is a payment or purchase; a card
    without that answer is never approved in bulk. Returns the operator's response:
    {"decision": "done" | "approve" | "reject", "note": str, "remember": bool}.
    """
    p = await db.pool()
    request = {"kind": kind, "message": message, "url": url, "requested_at": datetime.now(UTC).isoformat()}
    if offer:
        request["offer"] = offer
    if payment is not None:
        request["payment"] = payment
    await p.execute(
        "update subtasks set status = 'needs_human', human_request = $2, human_response = null where id = $1 and status = 'running'",
        subtask["id"],
        request,
    )
    await events.emit(
        "human.request", message, task_id=subtask["task_id"], subtask_id=subtask["id"], level="warn", data=request
    )
    deadline = time.monotonic() + settings().human_wait_seconds
    while time.monotonic() < deadline:
        await asyncio.sleep(2)
        r = await p.fetchrow("select status, human_response from subtasks where id = $1", subtask["id"])
        if r is None or r["status"] == "cancelled":
            raise Cancelled("Subtask was cancelled while waiting for the operator.")
        if r["human_response"] is not None:
            await p.execute("update subtasks set status = 'running' where id = $1 and status = 'needs_human'", subtask["id"])
            return r["human_response"]
    await p.execute("update subtasks set status = 'running' where id = $1 and status = 'needs_human'", subtask["id"])
    raise HumanTimeout(f"No answer from the operator within {settings().human_wait_seconds // 60} minutes.")


def bulk_allows(request: dict, decision: str) -> bool:
    """May this waiting card be answered together with others? Stopping always may. Approving only for what a click
    approves anyway: a saved login, or an approval that is explicitly NOT a payment. Never "I've handled it" (it
    claims the operator did something), never sign-ups, captchas or anything else."""
    if decision == "reject":
        return True
    kind = request.get("kind")
    return decision == "approve" and (kind == "login_saved" or (kind == "approval" and request.get("payment") is False))


async def respond_many(ids: list[UUID], decision: str, note: str = "") -> dict:
    """Answer several waiting cards at once: only the ones the operator was shown (their ids), only if each is
    eligible, each logged like a single answer. Returns {'answered': [...], 'skipped': [{'id', 'reason'}]}."""
    p = await db.pool()
    unique = list(dict.fromkeys(ids))
    waiting = {r["id"]: r["human_request"] for r in await p.fetch(
        "select id, human_request from subtasks where id = any($1) and status = 'needs_human' and human_response is null",
        unique)}
    answered, skipped = [], []
    for sid in unique:
        request = waiting.get(sid)
        if request is None:
            skipped.append({"id": sid, "reason": "not waiting any more"})
        elif not bulk_allows(request, decision):
            skipped.append({"id": sid, "reason": "needs your own answer"})
        elif await respond(sid, decision, note):
            answered.append(sid)
        else:
            skipped.append({"id": sid, "reason": "already answered"})
    return {"answered": answered, "skipped": skipped}


async def take_over_requested(subtask_id: UUID) -> bool:
    """Did the operator ask to take over this running step? Reads and clears the request (one pause per request)."""
    p = await db.pool()
    return bool(await p.fetchval("update subtasks set take_over = false where id = $1 and take_over returning true",
                                 subtask_id))


def take_over_message(profile_name: str | None) -> str:
    where = f"the browser window for profile “{profile_name}”" if profile_name else "the browser window"
    return (f"You took over this step. Do what you need in {where}, then click “Give control back” and Fleet "
            "carries on from whatever the page shows now. Nothing else runs in this window until you do.")


async def respond(subtask_id: UUID, decision: str, note: str = "", remember: bool = False) -> bool:
    p = await db.pool()
    r = await p.fetchrow(
        "update subtasks set human_response = $2 where id = $1 and status = 'needs_human' "
        "and human_response is null returning task_id",  # a double click must not answer twice
        subtask_id,
        {"decision": decision, "note": note, "remember": bool(remember and decision == "approve")},
    )
    if r is None:
        return False
    await events.emit(
        "human.response", f"Operator: {decision}" + (f" — {note}" if note else ""),
        task_id=r["task_id"], subtask_id=subtask_id, data={"decision": decision, "note": note},
    )
    return True
