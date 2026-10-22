"""The experience base: every browser step writes down where it was, what it was asked, what it did and how it ended
(READINESS.md, promise 4), and the base answers two questions about what it holds: "how did a goal like this go on
this site?" (`best_path`) and "what do we know about this site?" (`site_notes`).

What is stored is the step's own words and the engine's own words: the operator's instructions as the goal, the
replayable path (element labels and typed values, which the page reader never takes from password, card or code
fields), the outcome and its one-line reason, cost and time. Never page text, never a model's prose, never a secret
(reasons are passed through the vault's redaction like every event).

Both questions are answered by SQL and plain Python: no model is asked what a past run meant, and what comes back is
data for the caller to use as a hint (a path to try, a note to quote), never an instruction.

"The latest run" is `created_at desc, seq desc`: the clock has a step (15.6 ms on Windows), so two steps written in
the same tick carry the same timestamp, and `seq` - the order the rows were written in - decides which of them is the
later one. Without it the base is free to quote the second-to-last failure or replay the second-to-last path.
"""

from __future__ import annotations

import logging
import re
from urllib.parse import urlsplit

from fleet import db, vault

log = logging.getLogger("fleet.experience")

REASON_CHARS = 500
NOTES_CHARS = 600            # a note is one line the planner can afford to carry for every site a task names
SIMILAR_ENOUGH = 0.6         # shared words over the shorter goal, above which two goals are "a goal like this one"
PATH_ROWS = 50               # how far back best_path looks for a similar goal
NOTES_ROWS = 200             # how far back site_notes counts
FINISHED = ("done", "goal_achieved")
LOGIN_WORDS = ("login", "log in", "sign in", "sign-in", "signin", "logowan", "zaloguj")

# Words that say nothing about what a goal is about; dropped before two goals are compared.
STOP_WORDS = frozenset({
    "a", "about", "all", "an", "and", "any", "are", "as", "at", "be", "by", "can", "do", "does", "for", "from", "get",
    "go", "goes", "going", "had", "has", "have", "how", "if", "in", "into", "is", "it", "its", "me", "my", "of", "on",
    "only", "or", "our", "out", "over", "please", "so", "some", "that", "the", "their", "them", "then", "there",
    "these", "this", "to", "up", "us", "was", "we", "were", "what", "when", "where", "which", "who", "will", "with",
    "you", "your",
})


def domain_of(url: str | None) -> str:
    """The site a step worked on: the start page's host, lower-case, no port ("" when there is no usable URL)."""
    try:
        host = urlsplit(url or "").hostname or ""
    except ValueError:
        return ""
    return host.lower().rstrip(".")


async def record(ctx, status: str, reason: str | None, actions: list[dict] | None, seconds: float) -> None:
    """One row for this run of the step. Never raises: a problem writing experience must not fail the step itself."""
    st = ctx.subtask
    try:
        p = await db.pool()
        await p.execute(
            """
            insert into experience (domain, goal, kind, status, reason, actions, cost_usd, seconds, task_id, subtask_id)
            values ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
            """,
            domain_of(st.get("start_url")), st.get("instructions") or "", st.get("kind") or "browser", status,
            vault.redact(reason or "")[:REASON_CHARS], list(actions or []), round(float(ctx.usage.cost_usd), 4),
            float(max(seconds, 0.0)), st.get("task_id"), st.get("id"),
        )
    except Exception as e:  # noqa: BLE001 - logged, never raised
        log.warning("experience not recorded for step %s: %s", st.get("id"), e)


def _words(text: str) -> set[str]:
    """What a goal is about: its words, lower-case, without the ones that say nothing ("open the first result" ->
    {open, first, result}). Single letters are dropped as noise, single digits kept ("pi 5" is about the 5)."""
    return {w for w in re.findall(r"\w+", (text or "").lower())
            if w not in STOP_WORDS and (len(w) > 1 or w.isdigit())}


def _similarity(a: set[str], b: set[str]) -> float:
    """How much of the shorter goal the two share (1.0 when one goal's words are all in the other's)."""
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


async def best_path(domain: str, goal: str) -> list[dict] | None:
    """The actions of the latest step that finished on this site with a goal like this one, to be *tried* as a guide:
    None when the site is unknown, when nothing there got far enough to leave a path, or when every past goal was a
    different errand. A guide is a hint for the hands, never a promise that the page still looks the same."""
    words = _words(goal)
    if not domain or not words:
        return None
    try:
        p = await db.pool()
        rows = await p.fetch(
            "select goal, actions from experience "
            "where domain = $1 and status = any($2::text[]) and actions <> '[]'::jsonb "
            "order by created_at desc, seq desc limit $3",
            domain, list(FINISHED), PATH_ROWS)
    except Exception as e:  # noqa: BLE001 - a base that cannot be read is a base with nothing to say
        log.warning("experience not read for %s: %s", domain, e)
        return None
    for row in rows:
        if _similarity(words, _words(row["goal"])) >= SIMILAR_ENOUGH:
            return list(row["actions"] or [])
    return None


async def site_notes(domain: str) -> str:
    """One line about this site for the brain to carry: how many runs finished, how many did not, what the last one
    that did not said, and whether a login wall was met. Empty when nothing is known, never over NOTES_CHARS."""
    if not domain:
        return ""
    try:
        p = await db.pool()
        rows = await p.fetch(
            "select status, reason from experience where domain = $1 "
            "order by created_at desc, seq desc limit $2",
            domain, NOTES_ROWS)
    except Exception as e:  # noqa: BLE001 - as above: unreadable is the same as unknown
        log.warning("experience not read for %s: %s", domain, e)
        return ""
    if not rows:
        return ""
    finished = sum(1 for r in rows if r["status"] in FINISHED)
    head = f"{domain}: {finished} of {len(rows)} recent runs finished, {len(rows) - finished} did not."
    wall = " A login wall was met here." if any(
        w in (r["reason"] or "").lower() for r in rows for w in LOGIN_WORDS) else ""
    last = next((r for r in rows if r["status"] not in FINISHED and (r["reason"] or "").strip()), None)
    middle = ""
    if last:
        opening = f" Last one that did not ({last['status']}): "
        room = NOTES_CHARS - len(head) - len(wall) - len(opening) - 1
        reason = (last["reason"] or "").strip().rstrip(".")
        if room > 1:
            if len(reason) > room:
                reason = reason[:room - 1].rstrip() + "…"
            middle = opening + reason + "."
    return head + middle + wall
