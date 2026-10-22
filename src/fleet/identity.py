"""The operator's own details, used only to fill sign-up forms that the operator approved in the Inbox.

Real details of the real operator: never invented names, never other people's. Stored in `settings`.
"""

import re

from fleet import db

KEY = "identity"
FIELDS = ("first_name", "last_name", "email", "username")
EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,255}\.[^@\s]{2,}$")


class IdentityError(ValueError):
    pass


async def get() -> dict[str, str]:
    p = await db.pool()
    saved = await p.fetchval("select value from settings where key = $1", KEY) or {}
    return {f: str(saved.get(f, "")) for f in FIELDS}


async def put(values: dict[str, str]) -> dict[str, str]:
    clean = {f: (values.get(f) or "").strip()[:100] for f in FIELDS}
    clean["email"] = clean["email"][:320]
    if clean["email"] and not EMAIL_RE.match(clean["email"]):
        raise IdentityError("That doesn't look like an email address")
    if clean["username"] and not re.match(r"^[A-Za-z0-9._-]{3,40}$", clean["username"]):
        raise IdentityError("A username uses letters, digits, dots, dashes or underscores (3-40 characters)")
    p = await db.pool()
    await p.execute(
        "insert into settings (key, value) values ($1, $2) on conflict (key) do update set value = excluded.value, "
        "updated_at = now()", KEY, clean)
    return clean


def complete(identity: dict[str, str]) -> bool:
    """Fleet needs at least an email address to sign anyone up."""
    return bool(identity.get("email"))


def username_for(identity: dict[str, str]) -> str:
    """The operator's chosen username, else the part of their email before the @."""
    if identity.get("username"):
        return identity["username"]
    local = re.sub(r"[^A-Za-z0-9._-]", "", identity.get("email", "").split("@")[0])
    return local[:40] or "user"


# -- sites Fleet must never sign up on (whatever a task allows) ------------------------------------------------------------------

async def blocked_domains() -> list[str]:
    p = await db.pool()
    return [r["domain"] for r in await p.fetch("select domain from no_register_domains order by domain")]


async def block(domain: str) -> str:
    """Never sign up on this site (or its subdomains). The same address rules as saved logins: a real public hostname."""
    from fleet import site_accounts

    clean = site_accounts.normalize_domain(domain)
    p = await db.pool()
    await p.execute("insert into no_register_domains (domain) values ($1) on conflict do nothing", clean)
    return clean


async def unblock(domain: str) -> bool:
    p = await db.pool()
    return (await p.execute("delete from no_register_domains where domain = $1", domain.strip().lower())) == "DELETE 1"


async def is_blocked(host: str) -> bool:
    host = (host or "").lower().removeprefix("www.")
    return any(host == d or host.endswith("." + d) for d in await blocked_domains())

