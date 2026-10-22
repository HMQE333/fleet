"""Create an account on a site when the task needs one — only with the operator's say-so (RULES.md §1, §3-4).

The guard rails, all in code:
 - the TASK must allow registration (off by default);
 - the details are the operator's own (Settings → Your details), never invented;
 - EVERY sign-up is approved in the Inbox (no automatic mode), and the card lists exactly what will be filled
   and that the site's terms get accepted;
 - at most one account per site and profile, at most `registrations_per_day` per day, one attempt per run and site;
 - only forms Fleet fully understands are filled (see fastbrowser/register.py), once;
 - the generated password is saved encrypted BEFORE the form is submitted (never lost), shown only to the operator
   on request, and removed again if the site didn't accept the form;
 - email confirmation and captchas stay with the operator.
"""

import logging
import secrets
import string
from urllib.parse import urlsplit

from fleet import db, hitl, identity, limits, site_accounts
from fleet.executors import RunContext
from fleet.fastbrowser import login, register

log = logging.getLogger("fleet.registration")

PENDING = "Registered by Fleet (pending)"
DONE = "Registered by Fleet"
UNCONFIRMED = "Registered by Fleet (unconfirmed)"


def generate_password(length: int = 20) -> str:
    """Random, with a lower-case letter, an upper-case letter, a digit and a symbol (most sites' rules)."""
    classes = [string.ascii_lowercase, string.ascii_uppercase, string.digits, "!@#$%^&*-_"]
    chars = [secrets.choice(c) for c in classes] + [secrets.choice("".join(classes)) for _ in range(length - 4)]
    secrets.SystemRandom().shuffle(chars)
    return "".join(chars)


def _values(ident: dict[str, str], username: str, password: str) -> dict[str, str]:
    full = f"{ident['first_name']} {ident['last_name']}".strip()
    return {"first_name": ident["first_name"], "last_name": ident["last_name"], "full_name": full,
            "email": ident["email"], "email_confirm": ident["email"], "username": username,
            "password": password, "confirm": password}


class Registration:
    def __init__(self, ctx: RunContext):
        self.ctx = ctx
        self.profile_id = (ctx.profile or {}).get("id")
        self._tried: set[str] = set()
        self.note: str | None = None  # why the operator is being asked to sign up by hand

    def take_note(self) -> str | None:
        note, self.note = self.note, None
        return note

    async def _refuse(self, note: str) -> None:
        self.note = note
        await self.ctx.log(note, type="register", level="warn")

    async def _blocker(self, url: str, domain: str) -> str | None:
        """Why Fleet must not create an account right now, or None. Asked before the operator is, and again
        after they approve (another step may have signed up in the meantime)."""
        if await site_accounts.find_for_url(url, self.profile_id):
            return f"You already have a saved login for {domain}. Fleet doesn't create a second account."
        per_day = (await limits.effective()).registrations_per_day
        if await site_accounts.registrations_since(hours=24) >= per_day:
            return (f"Fleet already created {per_day} accounts in the last 24 hours, its "
                    "daily limit. Sign up by hand, or try again tomorrow.")
        return None

    async def try_register(self, send, url: str) -> str | None:
        """'reobserve' (the page changed: signed up, or the operator will), 'stop' (the operator stopped the step),
        or None (not our business, or refused: the operator is asked to sign up by hand)."""
        if not self.ctx.task.get("allow_registration"):
            return None
        host = (urlsplit(url).hostname or "").lower()
        if not host or host in self._tried:
            return None
        if (await login.classify(send)).get("kind") != "register":
            return None
        info = await register.plan(send)
        if info.get("kind") != "register":  # e.g. a change-password form
            return None
        self._tried.add(host)

        try:
            domain = site_accounts.normalize_domain(host)
        except site_accounts.SiteAccountError:
            await self._refuse(f"Fleet doesn't create accounts on {host}. Sign up by hand if you want one.")
            return None
        if await identity.is_blocked(domain):
            await self._refuse(f"You told Fleet never to sign up on {domain} (Settings → Your details). Sign up by hand if you want one.")
            return None
        if reason := await self._blocker(url, domain):
            await self._refuse(reason)
            return None
        if info.get("crossSite"):
            await self._refuse(f"This sign-up form sends your details to another site ({info.get('sendsTo')}), not "
                               f"{domain}. Fleet won't fill it. Sign up by hand if you trust it.")
            return None
        ident = await identity.get()
        if not identity.complete(ident):
            await self._refuse("Fleet needs your email address to sign up. Add your details in Settings, or sign up by hand.")
            return None
        if info.get("others"):
            await self._refuse(f"This sign-up form also requires {', '.join(info['others'])}, which Fleet doesn't "
                               "have. Sign up by hand.")
            return None
        missing = [r for r in register.NAME_ROLES if r in info["roles"] and info["required"].get(r)
                   and not _values(ident, "", "")[r]]
        if missing:
            await self._refuse("This sign-up form requires your name. Add it in Settings → Your details, or sign up by hand.")
            return None

        username = identity.username_for(ident)
        login_name = username if "username" in info["roles"] else ident["email"]
        will_fill = [r for r in ("first_name", "last_name", "full_name", "email", "username") if r in info["roles"]
                     and _values(ident, username, "")[r]]
        message = (f"Create an account on {domain} as {login_name}? Fleet will fill in: {', '.join(will_fill)}, and a "
                   "password it generates (saved encrypted; you can show it in Settings → Site logins)."
                   + (f" It will also tick: {'; '.join(info['terms'])}. Creating the account accepts the site's terms."
                      if info["terms"] else "")
                   + " Confirmation emails and captchas stay with you.")
        resp = await hitl.ask(self.ctx.subtask, "register", message, url=url)
        decision = resp.get("decision")
        if decision == "reject":
            return "stop"
        if decision != "approve":  # "I'll do it myself"
            return "reobserve"

        if reason := await self._blocker(url, domain):  # the operator may have taken a while to answer
            await self._refuse(reason)
            return None
        pool = await db.pool()  # an account is about to exist: this step must never be retried automatically
        await pool.execute("update subtasks set side_effects = true where id = $1", self.ctx.subtask["id"])
        password = generate_password()
        try:
            account = await site_accounts.create(domain, login_name, password, profile_id=self.profile_id,
                                                 label=PENDING, auto_login=False, origin="fleet")
        except site_accounts.SiteAccountError:  # a parallel step got there first
            await self._refuse(f"A saved login for {domain} appeared in the meantime. Fleet doesn't create a second account.")
            return None
        try:
            result = await register.fill(send, _values(ident, username, password))
        finally:
            del password
        if not result.accepted:
            if not result.submitted:  # nothing was sent, so no account exists
                await site_accounts.delete(account["id"])
                await self._refuse(f"Fleet couldn't fill in the sign-up form on {domain}, so nothing was submitted. "
                                   "Sign up by hand.")
                return None
            # Sent, but the page still shows the form: Fleet can't tell if the account exists. Keep the password.
            await site_accounts.update(account["id"], label=UNCONFIRMED)
            await self._refuse(f"Fleet submitted the sign-up form on {domain}, but the page still shows it (an error, "
                               "a taken username, or just slow), so Fleet can't tell whether the account exists. The "
                               "generated password is saved in Settings → Site logins; delete it there if the sign-up "
                               "failed. Check the page and finish by hand if needed.")
            return None
        await site_accounts.update(account["id"], label=DONE)
        await self.ctx.log(f"Created an account on {domain} as {login_name}. The generated password is saved "
                           "(Settings → Site logins → Show password).", type="register")
        if result.needs_verification:
            resp = await hitl.ask(
                self.ctx.subtask, "verify_email",
                f"{domain} says it sent a confirmation to {ident['email']}. Open that email, confirm the address, "
                "then click “I've handled it”.", url=url)
            if resp.get("decision") == "reject":
                return "stop"
        return "reobserve"
