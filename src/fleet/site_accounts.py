"""Saved logins: the operator's own username + password per site (and optionally per profile).

Passwords are encrypted with the vault's master key, bound to their row (a ciphertext can't be moved
to another login), and never leave this module except through `credentials()`, which only Fleet's
login code calls. No API response, event, trace or log contains one.
"""

import re
from urllib.parse import urlsplit
from uuid import UUID

from fleet import db, vault

PUBLIC_COLUMNS = (
    "id, domain, profile_id, label, username, origin, auto_login, created_at, updated_at, last_used_at, last_failed_at"
)
HOST_RE = re.compile(r"^(?=.{4,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")


class SiteAccountError(ValueError):
    pass


def normalize_domain(value: str) -> str:
    """'https://www.Example.com/login?x=1' → 'example.com'. Only real public hostnames are accepted."""
    raw = (value or "").strip().lower()
    host = urlsplit(raw if "://" in raw else f"//{raw}").hostname or ""
    host = host.removeprefix("www.")
    if not HOST_RE.match(host) or host.endswith((".local", ".localhost", ".internal", ".lan", ".home", ".arpa")):
        raise SiteAccountError("Enter the site's address, like example.com")
    return host


def _aad(account_id: UUID | str) -> str:
    return f"site-login:{account_id}"


async def list_accounts() -> list[dict]:
    p = await db.pool()
    rows = await p.fetch(
        f"select a.{PUBLIC_COLUMNS.replace(', ', ', a.')}, pr.name as profile_name "
        "from site_accounts a left join profiles pr on pr.id = a.profile_id order by a.domain, pr.name nulls first"
    )
    return db.rows(rows)


async def create(domain: str, username: str, password: str, *, profile_id: UUID | None = None, label: str = "",
                 auto_login: bool = False, origin: str = "operator") -> dict:
    domain = normalize_domain(domain)
    username = username.strip()
    if not username or not password:
        raise SiteAccountError("Both a username and a password are needed")
    p = await db.pool()
    if profile_id and await p.fetchval("select 1 from profiles where id = $1", profile_id) is None:
        raise SiteAccountError("No such profile")
    account_id = await p.fetchval("select gen_random_uuid()")
    ciphertext, nonce, key_id = vault.seal(_aad(account_id), password)
    try:
        row = await p.fetchrow(
            f"insert into site_accounts (id, domain, profile_id, label, username, ciphertext, nonce, key_id, origin, "
            f"auto_login) values ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10) returning {PUBLIC_COLUMNS}",
            account_id, domain, profile_id, label.strip()[:80], username, ciphertext, nonce, key_id, origin, auto_login,
        )
    except Exception as e:
        if "site_accounts_one" in str(e):
            raise SiteAccountError(f"There's already a saved login for {domain} here. Replace its password instead.") from e
        raise
    vault.remember(password, "SITE_LOGIN_PASSWORD")
    return dict(row)


async def update(account_id: UUID, *, password: str | None = None, username: str | None = None,
                 label: str | None = None, auto_login: bool | None = None) -> dict:
    p = await db.pool()
    if await p.fetchval("select 1 from site_accounts where id = $1", account_id) is None:
        raise SiteAccountError("No such saved login")
    if password is not None:
        if not password:
            raise SiteAccountError("The password is empty")
        ciphertext, nonce, key_id = vault.seal(_aad(account_id), password)
        await p.execute(
            "update site_accounts set ciphertext = $2, nonce = $3, key_id = $4, last_failed_at = null where id = $1",
            account_id, ciphertext, nonce, key_id,
        )
        vault.remember(password, "SITE_LOGIN_PASSWORD")
    row = await p.fetchrow(
        f"update site_accounts set username = coalesce($2, username), label = coalesce($3, label), "
        f"auto_login = coalesce($4, auto_login) where id = $1 returning {PUBLIC_COLUMNS}",
        account_id, (username or "").strip() or None, None if label is None else label.strip()[:80], auto_login,
    )
    return dict(row)


async def delete(account_id: UUID) -> bool:
    p = await db.pool()
    return (await p.execute("delete from site_accounts where id = $1", account_id)) == "DELETE 1"


async def find_for_url(url: str, profile_id: UUID | None) -> dict | None:
    """The login for this page's site: the profile's own first, then one for any profile; most specific domain."""
    host = (urlsplit(url).hostname or "").lower().removeprefix("www.")
    if not host:
        return None
    p = await db.pool()
    rows = await p.fetch(
        f"select {PUBLIC_COLUMNS} from site_accounts where profile_id is null or profile_id = $1", profile_id
    )
    matches = [r for r in rows if host == r["domain"] or host.endswith("." + r["domain"])]
    if not matches:
        return None
    matches.sort(key=lambda r: (r["profile_id"] is None, -len(r["domain"])))
    return dict(matches[0])


async def credentials(account_id: UUID) -> tuple[str, str]:
    """(username, password) for Fleet's login code. Never log, store or return these."""
    p = await db.pool()
    r = await p.fetchrow("select username, ciphertext, nonce from site_accounts where id = $1", account_id)
    if r is None:
        raise SiteAccountError("The saved login no longer exists")
    try:
        password = vault.unseal(_aad(account_id), r["ciphertext"], r["nonce"])
    except vault.VaultError as e:
        raise SiteAccountError(f"The saved password can't be read ({e}). Save it again in Settings → Site logins.") from e
    await p.execute("update site_accounts set last_used_at = now() where id = $1", account_id)
    return r["username"], password


async def registrations_since(*, hours: int) -> int:
    """Accounts Fleet created itself recently (the daily limit counts these)."""
    p = await db.pool()
    return await p.fetchval(
        "select count(*) from site_accounts where origin = 'fleet' and created_at > now() - make_interval(hours => $1)",
        hours)


async def reveal(account_id: UUID) -> tuple[str, str]:
    """(domain, password) of an account FLEET created, for its owner to read. Passwords the operator typed in
    stay write-only. Doesn't count as a use of the login."""
    p = await db.pool()
    r = await p.fetchrow("select domain, origin, ciphertext, nonce from site_accounts where id = $1", account_id)
    if r is None:
        raise SiteAccountError("No such saved login")
    if r["origin"] != "fleet":
        raise SiteAccountError("Only passwords Fleet generated can be shown. Passwords you saved yourself are write-only.")
    try:
        return r["domain"], vault.unseal(_aad(account_id), r["ciphertext"], r["nonce"])
    except vault.VaultError as e:
        raise SiteAccountError(f"The saved password can't be read ({e}).") from e


async def mark_failed(account_id: UUID) -> None:
    p = await db.pool()
    await p.execute("update site_accounts set last_failed_at = now() where id = $1", account_id)
