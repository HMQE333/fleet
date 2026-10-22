"""Log in with a saved login — typed by code, never by a model.

Works through any `send(method, params)` coroutine that talks to one page's CDP session (the fast
engine's Page, or browser-use's session), so both browser workers share it.

A saved login is only ever typed into a LOGIN form. The page script classifies the form first:
sign-up forms (`autocomplete=new-password`, a confirm-password field, a sign-up submit button,
heading or URL) and change-password forms (several password fields) are left alone.
"""

import asyncio
import json
import logging

log = logging.getLogger("fleet.login")

# Finds the visible password field, classifies the form, and keeps the elements on window.__fleetLogin.
CLASSIFY_JS = r"""(() => {
  window.__fleetLogin = null;
  const visible = e => !e.closest('[aria-hidden="true"],[inert]') &&
    e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true});
  const usable = e => { if (!e || !visible(e) || e.disabled || e.readOnly) return false;
    const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  const passwords = [...document.querySelectorAll('input[type="password"]')].filter(usable);
  if (!passwords.length) return {kind: 'none'};
  const P = passwords[0];
  const scope = P.form || P.closest('form,dialog,[role="dialog"],main') || document.body;
  const SIGNUP = /sign ?up|register|create (an? )?account|join( now)?|new account|zarejestruj|załóż|utwórz konto/i;
  const LOGIN = /log ?in|sign ?in|zaloguj|logowanie/i;
  const text = e => (e.innerText || e.value || e.getAttribute('aria-label') || '').trim().toLowerCase();
  const after = e => !!(P.compareDocumentPosition(e) & Node.DOCUMENT_POSITION_FOLLOWING);
  const buttons = [...scope.querySelectorAll('button,input[type="submit"],[role="button"]')].filter(visible);
  const submit = [...scope.querySelectorAll('button[type="submit"],input[type="submit"]')].filter(visible)[0] ||
    buttons.filter(after).find(b => LOGIN.test(text(b))) || null;
  const submitText = submit ? text(submit) : '';
  const where = location.pathname + ' ' + (P.form ? P.form.action : '');
  const heading = [...scope.querySelectorAll('h1,h2,h3,legend')].filter(visible).map(text).join(' ');
  const autocomplete = (P.getAttribute('autocomplete') || '').toLowerCase();
  // Only password fields of THIS form: pages like news.ycombinator.com/login hold a login form and a sign-up form side by side.
  const confirmField = passwords.filter(p => scope.contains(p)).length > 1 ||
    /confirm|repeat|retype|powtórz/i.test(P.name + ' ' + P.id + ' ' + (P.getAttribute('aria-label') || ''));
  const looksSignup = SIGNUP.test(submitText) || (!LOGIN.test(submitText) && (SIGNUP.test(where) || SIGNUP.test(heading)));
  const register = autocomplete === 'new-password' || confirmField || looksSignup;
  const skip = ['password','hidden','checkbox','radio','submit','button','file','image','reset','range','color','date'];
  const before = [...scope.querySelectorAll('input,textarea')]
    .filter(e => usable(e) && !skip.includes(e.type) && (e.compareDocumentPosition(P) & Node.DOCUMENT_POSITION_FOLLOWING));
  const named = e => e.type === 'email' || /user|email|login|mail|nick/i.test(e.name + ' ' + e.id + ' ' + (e.getAttribute('autocomplete') || ''));
  const U = before.filter(named).pop() || before.pop() || null;
  if (!register) window.__fleetLogin = {U, P, S: submit};
  return {kind: register ? 'register' : 'login', user: !!U, submit: !!submit};
})()"""

FOCUS_JS = r"""((which) => { const c = window.__fleetLogin; const el = c && c[which];
  if (!el || !el.isConnected) return false;
  el.scrollIntoView({block: 'center'}); el.focus(); if (typeof el.select === 'function') el.select();
  return document.activeElement === el; })(__WHICH__)"""

SUBMIT_JS = r"""(() => { const c = window.__fleetLogin; if (!c || !c.S || !c.S.isConnected) return false;
  c.S.click(); return true; })()"""


class LoginError(RuntimeError):
    pass


async def _evaluate(send, expression: str):
    result = await send("Runtime.evaluate", {"expression": expression, "returnByValue": True})
    if result.get("exceptionDetails"):
        raise LoginError("the page changed while Fleet was reading it")
    return result.get("result", {}).get("value")


async def classify(send) -> dict:
    """{'kind': 'login' | 'register' | 'none', ...}. Any failure counts as 'none' (the page may be navigating)."""
    try:
        return await _evaluate(send, CLASSIFY_JS) or {"kind": "none"}
    except Exception as e:
        log.debug("login check skipped: %s", e)
        return {"kind": "none"}


async def _focus(send, which: str) -> bool:
    return bool(await _evaluate(send, FOCUS_JS.replace("__WHICH__", json.dumps(which))))


async def _press_enter(send) -> None:
    for kind in ("keyDown", "keyUp"):
        params = {"type": kind, "key": "Enter", "code": "Enter", "windowsVirtualKeyCode": 13}
        if kind == "keyDown":
            params["text"] = "\r"
        await send("Input.dispatchKeyEvent", params)


async def perform(send, username: str, password: str, *, settle_seconds: float = 4.0) -> bool:
    """Type the login into the page's login form and submit it. True when the login form is gone afterwards.

    Refuses (False) unless the page really is a login form. Makes exactly one submission.
    """
    info = await classify(send)
    if info.get("kind") != "login":
        return False
    if info.get("user"):
        if not await _focus(send, "U"):
            return False
        await send("Input.insertText", {"text": username})
    if not await _focus(send, "P"):
        return False
    await send("Input.insertText", {"text": password})
    if not (info.get("submit") and await _evaluate(send, SUBMIT_JS)):
        await _press_enter(send)
    deadline = asyncio.get_running_loop().time() + settle_seconds
    while asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.25)
        if (await classify(send)).get("kind") != "login":
            return True
    return False


# -- two-step logins: a username page first, the password page after ------------------------------------------------
#
# Many sites (and most big providers) ask for the username on one page and the password on the next. The first page has
# no password field, so it can only be told apart from a newsletter box, a search or a contact form by its context.
# It counts as a username step only when ALL of these hold: exactly one visible identifier field (username / email /
# login...) and nothing else to fill in; the page, heading or button talks about signing in; it doesn't talk about
# signing up, subscribing or a newsletter. Even then Fleet only looks when the operator saved a login for the site.

STEP_JS = r"""(() => {
  window.__fleetLogin = null;
  const visible = e => !e.closest('[aria-hidden="true"],[inert]') &&
    e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true});
  const usable = e => { if (!e || !visible(e) || e.disabled || e.readOnly) return false;
    const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  if ([...document.querySelectorAll('input[type="password"]')].some(usable)) return {kind: 'none'};
  const ac = e => (e.getAttribute('autocomplete') || '').toLowerCase();
  const labelOf = e => ([...(e.labels || [])].map(l => l.innerText).join(' ') || e.getAttribute('aria-label') ||
    e.placeholder || '').replace(/\s+/g, ' ').trim();
  const hay = e => [e.name, e.id, e.placeholder, e.getAttribute('aria-label'), ac(e), labelOf(e)].join(' ').toLowerCase();
  const SEARCHY = /search|query|szukaj|newsletter|subscribe|coupon|promo|comment|message|captcha|zip|postal|phone/;
  const IDENT = /user|e-?mail|login|account|identifier|konto|użytkownik|uzytkownik|nick/;
  const textual = e => ['text', 'email', ''].includes((e.getAttribute('type') || '').toLowerCase());
  const idents = [...document.querySelectorAll('input')].filter(e => usable(e) && textual(e) && !SEARCHY.test(hay(e)) &&
    !e.closest('[role="search"]') && (IDENT.test(hay(e)) || ac(e).includes('username')));
  if (idents.length !== 1) return {kind: 'none'};
  const U = idents[0];
  const scope = U.form || U.closest('form,dialog,[role="dialog"],main') || document.body;
  const others = [...scope.querySelectorAll('input,textarea,select')].filter(e => usable(e) && e !== U &&
    !['hidden', 'submit', 'button', 'image', 'reset', 'checkbox'].includes(e.type) && !/search|query|szukaj/.test(hay(e)) &&
    !e.closest('[role="search"]'));
  if (others.length) return {kind: 'none'};
  const text = e => (e.innerText || e.value || e.getAttribute('aria-label') || '').replace(/\s+/g, ' ').trim().toLowerCase();
  const NEXT = /next|continue|go|submit|log ?in|sign ?in|dalej|kontynuuj|zaloguj|identifier/;
  const buttons = [...scope.querySelectorAll('button,input[type="submit"],[role="button"]')].filter(visible);
  const submit = [...scope.querySelectorAll('button[type="submit"],input[type="submit"]')].filter(visible)[0] ||
    buttons.filter(b => U.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING).find(b => NEXT.test(text(b))) || null;
  const submitText = submit ? text(submit) : '';
  const heading = [...scope.querySelectorAll('h1,h2,h3,legend')].concat([...document.querySelectorAll('h1')])
    .filter(visible).map(text).join(' ');
  const where = location.pathname.toLowerCase();
  const LOGIN = /log ?in|sign ?in|zaloguj|logowanie/;
  const SIGNUP = /sign ?up|register|create (an? )?account|join|subscribe|newsletter|zarejestruj|załóż|utwórz konto|zapisz się/;
  const loginish = LOGIN.test(heading + ' ' + document.title.toLowerCase() + ' ' + where + ' ' + submitText);
  if (!loginish || SIGNUP.test(submitText + ' ' + where) || /newsletter|subscribe/.test((scope.innerText || '').toLowerCase().slice(0, 600)))
    return {kind: 'none'};
  window.__fleetLogin = {U, S: submit};
  return {kind: 'login_step', submit: !!submit};
})()"""


async def classify_step(send) -> dict:
    """{'kind': 'login_step' | 'none', ...}: is this the username page of a two-step login? Failure counts as 'none'."""
    try:
        return await _evaluate(send, STEP_JS) or {"kind": "none"}
    except Exception as e:
        log.debug("login step check skipped: %s", e)
        return {"kind": "none"}


async def perform_step(send, username: str, *, settle_seconds: float = 5.0) -> bool:
    """Type the username on a username page and continue. True when a login (password) form shows up next.

    Refuses (False) unless the page really is a username step. Makes exactly one submission.
    """
    info = await classify_step(send)
    if info.get("kind") != "login_step" or not await _focus(send, "U"):
        return False
    await send("Input.insertText", {"text": username})
    if not (info.get("submit") and await _evaluate(send, SUBMIT_JS)):
        await _press_enter(send)
    deadline = asyncio.get_running_loop().time() + settle_seconds
    while asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.25)
        if (await classify(send)).get("kind") == "login":
            return True
    return False


async def perform_two_step(send, username: str, password: str, *, settle_seconds: float = 4.0) -> bool:
    """Username page, then password page. True when the login form is gone at the end."""
    return await perform_step(send, username, settle_seconds=settle_seconds + 1) and await perform(
        send, username, password, settle_seconds=settle_seconds)
