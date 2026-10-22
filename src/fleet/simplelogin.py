"""SimpleLogin: email aliases for sign-ups (https://simplelogin.io). An alias forwards to the operator's own mailbox. Fleet
asks for a new random alias and the operator puts it into a sign-up form they approved.

Creating an alias changes no account and sends no mail. Accounts stay under RULES.md §1: a sign-up still needs the
operator's approval in the Inbox, at most one per site and profile, within the daily limit.

The API (POST /api/alias/random/new, key in the `Authentication` header) is described in the SimpleLogin docs and in
the community clients; the key is read from SIMPLELOGIN_API_KEY (vault first, then the environment, like every key).
"""

from __future__ import annotations

import re

import httpx

from fleet import vault

BASE_URL = "https://app.simplelogin.io/api"
KEY_NAME = "SIMPLELOGIN_API_KEY"
NOTE_CHARS = 200
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_TRANSPORT: httpx.AsyncBaseTransport | None = None  # tests only: a mock transport in place of the network


class AliasError(RuntimeError):
    """SimpleLogin refused or could not be reached. The message is written for the operator and never holds the key."""


def _detail(r: httpx.Response) -> str:
    try:
        text = str(r.json().get("error") or "")
    except ValueError:
        text = r.text
    return vault.redact(text.strip())[:200]


async def new_random_alias(api_key: str, *, note: str = "", hostname: str = "", mode: str = "word") -> str:
    """A new random alias address from SimpleLogin. `note` is kept with the alias on SimpleLogin's side (what it's for),
    `hostname` names the site it will be given to, `mode` is "word" or "uuid"."""
    if not api_key:
        raise AliasError("No SimpleLogin API key yet. Put it in with scripts\\set_key.ps1 SIMPLELOGIN_API_KEY.")
    params: dict[str, str] = {}
    if hostname.strip():
        params["hostname"] = hostname.strip()[:200]
    if mode in ("word", "uuid"):
        params["mode"] = mode
    body = {"note": note.strip()[:NOTE_CHARS]} if note.strip() else {}
    try:
        async with httpx.AsyncClient(base_url=BASE_URL, timeout=20, transport=_TRANSPORT) as client:
            r = await client.post("/alias/random/new", params=params, json=body, headers={"Authentication": api_key})
    except httpx.HTTPError as e:
        raise AliasError(f"Couldn't reach SimpleLogin ({type(e).__name__}). Check the internet connection.") from e
    if r.status_code == 401:
        raise AliasError("SimpleLogin rejected the API key. Put a current one in with scripts\\set_key.ps1 SIMPLELOGIN_API_KEY.")
    if r.status_code == 429:
        raise AliasError("SimpleLogin is limiting requests for this key right now. Try again in a few minutes.")
    if r.status_code >= 400:
        detail = _detail(r)
        raise AliasError(f"SimpleLogin refused to create an alias (HTTP {r.status_code})" + (f": {detail}" if detail else "."))
    try:
        email = str((r.json() or {}).get("email") or "")
    except ValueError:
        email = ""
    if not EMAIL_RE.match(email):
        raise AliasError("SimpleLogin answered without an alias address.")
    return email


async def create(*, note: str = "", hostname: str = "") -> str:
    """The same, with the key read the way every Fleet key is read (vault, then environment)."""
    try:
        key = await vault.get(KEY_NAME)
    except vault.VaultError as e:
        raise AliasError(str(e)) from e
    return await new_random_alias(key or "", note=note, hostname=hostname)
