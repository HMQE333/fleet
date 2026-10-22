"""When a task reaches a login form and the operator saved a login for that site: use it, their way.

Shared by both browser workers. The operator stays in control (RULES.md §3-4):
 - unless that login is marked "automatic", the Inbox asks first ("Log in with the saved login?")
   and "I'll do it myself" is always an option;
 - one attempt per run and site, so a wrong password can never become a lockout loop;
 - only login forms are ever filled (see fastbrowser/login.py), never sign-up forms;
 - the password goes from the encrypted store straight to the page — no model, prompt, event or log sees it.
"""

import logging
from urllib.parse import urlsplit

from fleet import hitl, site_accounts
from fleet.executors import RunContext
from fleet.fastbrowser import login

log = logging.getLogger("fleet.saved_login")


def cdp_sender(cdp_session):
    """Adapt a browser-use CDP session to the `send(method, params)` shape the login code uses."""

    async def send(method: str, params: dict | None = None) -> dict:
        domain, name = method.split(".", 1)
        call = getattr(getattr(cdp_session.cdp_client.send, domain), name)
        return await call(params=params or {}, session_id=cdp_session.session_id)

    return send


class SavedLogin:
    def __init__(self, ctx: RunContext):
        self.ctx = ctx
        self.profile_id = (ctx.profile or {}).get("id")
        self._tried: set[str] = set()
        self.note: str | None = None  # why the operator is being asked to log in by hand

    def take_note(self) -> str | None:
        note, self.note = self.note, None
        return note

    async def watching(self, url: str) -> bool:
        """True when a saved login exists for this page's site and hasn't been tried yet in this run: worth
        looking for the username page of a two-step login even though no password field is showing."""
        host = (urlsplit(url).hostname or "").lower()
        return bool(host) and host not in self._tried and await site_accounts.find_for_url(url, self.profile_id) is not None

    async def try_login(self, send, url: str) -> str | None:
        """'reobserve' (logged in, or the operator will), 'stop' (the operator stopped the step) or
        None (nothing to do here: no saved login, a sign-up form, or it didn't work — hand over to the operator).

        Handles one-page logins and two-step ones (a username page, then a password page)."""
        host = (urlsplit(url).hostname or "").lower()
        if not host or host in self._tried:
            return None
        kind = (await login.classify(send)).get("kind")
        if kind not in ("login", "none"):  # a sign-up or change-password form is never ours
            return None
        account = await site_accounts.find_for_url(url, self.profile_id)
        if account is None:
            return None
        two_step = kind == "none"  # no password field yet: maybe the username page of a two-step login
        if two_step and (await login.classify_step(send)).get("kind") != "login_step":
            return None
        self._tried.add(host)

        who = f"{account['username']} on {account['domain']}"
        if not account["auto_login"]:
            resp = await hitl.ask(self.ctx.subtask, "login_saved", f"Log in as {who} with the saved login?", url=url)
            decision = resp.get("decision")
            if decision == "reject":
                return "stop"
            if decision != "approve":  # "I'll do it myself"
                return "reobserve"

        username, password = await site_accounts.credentials(account["id"])
        try:
            ok = await (login.perform_two_step if two_step else login.perform)(send, username, password)
        finally:
            del password
        if ok:
            how = " (automatic)" if account["auto_login"] else ""
            await self.ctx.log(f"Logged in as {who} with the saved login{how}" + (" in two steps" if two_step else ""),
                               type="login")
            return "reobserve"

        await site_accounts.mark_failed(account["id"])
        self.note = (f"The saved login for {account['domain']} didn't work (the site still shows its login form). "
                     "Fleet won't try again. Check it in Settings → Site logins, or log in by hand.")
        await self.ctx.log(self.note, type="login", level="warn")
        return None
