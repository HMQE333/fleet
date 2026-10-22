"""Opt-in notifications: a short message to a webhook the operator entered, so Fleet can tell them when something needs them.

Off by default and entirely the operator's choice: nothing leaves this machine until they paste an address in Settings → Notifications,
tick what they want to hear about, and press "Send a test". What goes out is one short line per event (what needs you, what changed,
which task finished), never page content beyond that line, never a password or a key (`vault.redact` runs over the text).

Safety of the address, which is the operator's own but still must not become a way to reach their network:
 - https only, no embedded credentials, a public host on port 443, checked again after DNS at SEND time;
 - redirects are never followed;
 - the address is a secret (webhook addresses carry a token): stored encrypted in the vault, never returned by the API, only its host is shown;
 - at most 20 messages an hour, and a failed delivery is logged (without the address) and never blocks or breaks the work.
"""

import asyncio
import logging
import time
from urllib.parse import urlsplit

import httpx

from fleet import db, vault
from fleet.fastbrowser import netguard

log = logging.getLogger("fleet.notify")

SECRET = "NOTIFY_WEBHOOK_URL"
SETTINGS_KEY = "notifications"
TRIGGERS = {
    "needs_you": "Something needs you (a card in the Inbox)",
    "updates": "A saved automation found changes or crossed a watch line",
    "finished": "A task finished or failed",
}
FORMATS = ("json", "plain")
MAX_PER_HOUR = 20
MAX_TEXT = 400
ALLOW_PLAIN_HTTP = False  # tests only

_cache: tuple[float, dict] | None = None
_sent: list[float] = []
_paused_until = 0.0
_tasks: set[asyncio.Task] = set()


class NotifyError(ValueError):
    pass


def _forget() -> None:
    global _cache
    _cache = None


async def _address() -> str | None:
    return await vault.get(SECRET)


async def config() -> dict:
    """What the panel shows: whether it's set up, the host only, what to tell and in which format. Never the address."""
    p = await db.pool()
    saved = await p.fetchval("select value from settings where key = $1", SETTINGS_KEY) or {}
    url = await _address()
    return {"configured": bool(url), "host": (urlsplit(url).hostname if url else None),
            "on": [t for t in saved.get("on", []) if t in TRIGGERS], "format": saved.get("format", "json"),
            "triggers": [{"name": k, "label": v} for k, v in TRIGGERS.items()]}


async def _problem(url: str) -> str | None:
    parts = urlsplit(url)
    if parts.scheme != "https" and not (ALLOW_PLAIN_HTTP and parts.scheme == "http"):
        return "Use an https address"
    return await netguard.check_url_resolved(url)


async def save(*, url: str | None, on: list[str], fmt: str) -> dict:
    """`url`: None keeps the saved address, "" removes it, anything else replaces it (checked first)."""
    unknown = [t for t in on if t not in TRIGGERS]
    if unknown:
        raise NotifyError(f"Unknown thing to tell you about: {', '.join(unknown)}")
    if fmt not in FORMATS:
        raise NotifyError("The format is json or plain")
    if url:
        url = url.strip()
        if len(url) > 2000 or (reason := await _problem(url)):
            raise NotifyError(f"That address can't be used: {reason if len(url) <= 2000 else 'it is too long'}")
        await vault.put(SECRET, url)
    elif url == "":
        await vault.delete(SECRET)
    p = await db.pool()
    await p.execute(
        "insert into settings (key, value) values ($1, $2) on conflict (key) do update set value = excluded.value, updated_at = now()",
        SETTINGS_KEY, {"on": sorted(set(on)), "format": fmt})
    _forget()
    return await config()


async def send(text: str, *, title: str = "Fleet") -> None:
    """Deliver one message now. Raises NotifyError (without the address in it) when it can't."""
    url = await _address()
    if not url:
        raise NotifyError("No notification address is set")
    if reason := await _problem(url):  # again, at send time: DNS may have changed since it was saved
        raise NotifyError(f"The address is no longer allowed: {reason}")
    text = vault.redact(text)[:MAX_TEXT]
    fmt = (await config())["format"]
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
            if fmt == "plain":
                r = await client.post(url, content=text.encode("utf-8"), headers={"Title": "Fleet", "Content-Type": "text/plain; charset=utf-8"})
            else:
                r = await client.post(url, json={"text": text, "content": text, "title": title, "source": "fleet"})
    except httpx.HTTPError as e:
        raise NotifyError(f"The notification could not be delivered ({type(e).__name__})") from None
    if r.status_code >= 300:
        raise NotifyError(f"The notification address answered HTTP {r.status_code}")


def _allowed_now() -> str:
    """"ok", "paused" (still waiting for the hour to pass) or "pausing" (this one went over the limit: say so once).
    At most MAX_PER_HOUR messages an hour."""
    global _paused_until
    now = time.monotonic()
    if now < _paused_until:
        return "paused"
    _sent[:] = [t for t in _sent if now - t < 3600]
    if len(_sent) >= MAX_PER_HOUR:
        _paused_until = _sent[0] + 3600
        return "pausing"
    _sent.append(now)
    return "ok"


async def _settings() -> dict:
    """The saved choices, remembered for half a minute (events are frequent; this must stay cheap)."""
    global _cache
    if _cache and time.monotonic() - _cache[0] < 30:
        return _cache[1]
    c = await config()
    value = {"configured": c["configured"], "on": set(c["on"])}
    _cache = (time.monotonic(), value)
    return value


def _line(type_: str, message: str, data: dict, prompt: str) -> tuple[str, str] | None:
    """(trigger, text) for the events worth a message, else None."""
    task = f" [{prompt[:60]}]" if prompt else ""
    if type_ == "human.request":
        return "needs_you", f"Needs you{task}: {message}"
    if type_ == "scrape.changes":
        return "updates", f"Update{task}: {message}"
    if type_ == "task.status" and data.get("status") in ("done", "failed"):
        return "finished", f"Task {data['status']}{task}"
    return None


async def on_event(type_: str, message: str, task_id, data: dict) -> None:
    """Called for every event; does nothing (cheaply) unless notifications are set up and this kind of event is wanted. Never raises."""
    try:
        wanted = await _settings()
        if not wanted["configured"] or not wanted["on"]:
            return
        prompt = ""
        if task_id is not None and type_ in ("human.request", "scrape.changes", "task.status"):
            p = await db.pool()
            prompt = (await p.fetchval("select prompt from tasks where id = $1", task_id)) or ""
        line = _line(type_, message, data or {}, " ".join(prompt.split()))
        if line is None or line[0] not in wanted["on"]:
            return
        gate = _allowed_now()
        if gate == "pausing":
            from fleet import events

            await events.emit("notify.paused", f"More than {MAX_PER_HOUR} notifications in an hour: paused until the hour is up", level="warn")
        if gate != "ok":
            return
        await send(line[1])
    except Exception as e:  # a notification problem must never touch the work
        log.warning("notification not sent: %s", e)
        try:
            from fleet import events

            await events.emit("notify.failed", f"A notification could not be sent: {e}", level="warn")
        except Exception:
            pass


WATCHED = {"human.request", "scrape.changes", "task.status"}  # the only events that can ever become a message


def spawn(type_: str, message: str, task_id, data: dict) -> None:
    """Fire and forget from `events.emit`, so a slow webhook never delays the work."""
    if type_ not in WATCHED or (type_ == "task.status" and (data or {}).get("status") not in ("done", "failed")):
        return
    task = asyncio.get_running_loop().create_task(on_event(type_, message, task_id, data))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
