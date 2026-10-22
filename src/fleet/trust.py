"""Trust rules: the operator's standing OK for one exact click on one exact site, for one profile.

When the Inbox asks "Click “Send” on mail.example.com?" the operator may answer "approve and always allow this".
Next time the same label shows up on the same site in the same profile, the fast browser clicks without asking, and
says so in the activity feed. This is how a task that runs every day stops needing a human for routine clicks
while everything new still asks (RULES.md §4).

Deliberately narrow:
 - the match is exact (normalised label, exact host, this profile only), never a pattern;
 - rules are created only from an approval card the operator answered, never typed in or imported;
 - some clicks can never be trusted: payments, deleting, accepting terms, signing up, generic labels such as
   "OK" or "Confirm" whose meaning depends on the page (`fleet/fastbrowser/policy.py: can_trust`);
 - captchas, logins and sign-ups are different Inbox kinds and are not covered by rules at all.
"""

import re
from urllib.parse import urlsplit
from uuid import UUID

from fleet import db, site_accounts
from fleet.fastbrowser import policy

COLUMNS = "id, profile_id, domain, label, uses, last_used_at, created_at"


class TrustError(ValueError):
    pass


def normalize_label(label: str) -> str:
    return re.sub(r"\s+", " ", label or "").strip().casefold()[:120]


def offer(url: str, label: str) -> dict | None:
    """What an approval card may offer to remember, or None when this click must always ask."""
    if not policy.can_trust(label):
        return None
    try:
        domain = site_accounts.normalize_domain(url)
    except site_accounts.SiteAccountError:
        return None
    return {"domain": domain, "label": re.sub(r"\s+", " ", label).strip()[:120]}


async def add(profile_id: UUID, domain: str, label: str) -> dict:
    """Remember an approved click. Idempotent: the same rule twice is one rule."""
    if not policy.can_trust(label):
        raise TrustError("That click can't be trusted automatically; it will always ask.")
    key = normalize_label(label)
    p = await db.pool()
    row = await p.fetchrow(
        f"insert into trust_rules (profile_id, domain, label, key) values ($1, $2, $3, $4) "
        f"on conflict (profile_id, domain, key) do update set label = trust_rules.label returning {COLUMNS}",
        profile_id, domain, re.sub(r"\s+", " ", label).strip()[:120], key)
    return dict(row)


async def find(url: str, label: str, profile_id: UUID | None) -> dict | None:
    """The rule that covers this exact click, if any."""
    host = (urlsplit(url).hostname or "").lower().removeprefix("www.")
    if not host or profile_id is None or not policy.can_trust(label):
        return None
    p = await db.pool()
    row = await p.fetchrow(
        f"select {COLUMNS} from trust_rules where profile_id = $1 and domain = $2 and key = $3",
        profile_id, host, normalize_label(label))
    return dict(row) if row else None


async def used(rule_id: UUID) -> None:
    p = await db.pool()
    await p.execute("update trust_rules set uses = uses + 1, last_used_at = now() where id = $1", rule_id)


async def list_rules() -> list[dict]:
    p = await db.pool()
    return db.rows(await p.fetch(
        "select r.id, r.profile_id, pr.name as profile_name, r.domain, r.label, r.uses, r.last_used_at, r.created_at "
        "from trust_rules r join profiles pr on pr.id = r.profile_id order by r.domain, r.label"))


async def delete(rule_id: UUID) -> bool:
    p = await db.pool()
    return (await p.execute("delete from trust_rules where id = $1", rule_id)) == "DELETE 1"
