"""Fill a sign-up form with the operator's own details — typed by code, after the operator approved in the Inbox.

Works through any `send(method, params)` coroutine for one page's CDP session (same as login.py).
Fleet only fills a form it fully understands:
 - it must be a sign-up form (not a login or change-password form: `login.classify` and this plan agree);
 - every REQUIRED field must be one Fleet has an honest value for (email, username, name, password).
   A required phone number, address, birth date, dropdown or the like means: don't touch it, the operator signs up;
 - only terms/privacy checkboxes are ticked (the approval card says so); newsletter and marketing boxes never are.
"""

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field

from fleet.fastbrowser.login import _evaluate, _press_enter, classify

log = logging.getLogger("fleet.register")

NAME_ROLES = ("first_name", "last_name", "full_name")
FILL_ORDER = ("first_name", "last_name", "full_name", "email", "email_confirm", "username", "password", "confirm")
VERIFY_RE = re.compile(
    r"verify (your )?e-?mail|confirm (your )?e-?mail|check your (e-?mail|inbox)|confirmation (link|e-?mail)|"
    r"verification (link|e-?mail)|sent (you )?(a|an) (link|e-?mail)|potwierdź.{0,20}(e-?mail|adres)|"
    r"sprawdź.{0,20}(skrzynk|e-?mail)|wysłaliśmy", re.I)

PLAN_JS = r"""(() => {
  window.__fleetSignup = null;
  const visible = e => !e.closest('[aria-hidden="true"],[inert]') &&
    e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true});
  const usable = e => { if (!e || !visible(e) || e.disabled || e.readOnly) return false;
    const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  const ac = e => (e.getAttribute('autocomplete') || '').toLowerCase();
  const passwords = [...document.querySelectorAll('input[type="password"]')].filter(usable);
  if (!passwords.length) return {kind: 'none'};
  // A page with any current-password field is a login or change-password page, never a sign-up. This also keeps this plan in
  // step with login.classify, which looks at the first password field: both must agree before anything is filled.
  if (passwords.some(e => ac(e) === 'current-password')) return {kind: 'change'};
  const P = passwords[0];
  const scope = P.form || P.closest('form,dialog,[role="dialog"],main') || document.body;
  const labelOf = e => ([...(e.labels || [])].map(l => l.innerText).join(' ') || e.getAttribute('aria-label') ||
    e.placeholder || '').replace(/\s+/g, ' ').trim();
  const hay = e => [e.name, e.id, e.placeholder, e.getAttribute('aria-label'), ac(e), labelOf(e)].join(' ').toLowerCase();
  const map = {}, roles = [], required = {}, terms = [], others = [];
  const add = (role, el, req) => { if (map[role]) return false; map[role] = el; roles.push(role); required[role] = req; return true; };
  for (const e of scope.querySelectorAll('input,select,textarea')) {
    if (!usable(e)) continue;
    const type = (e.type || '').toLowerCase();
    if (['hidden','submit','button','image','reset','file','search'].includes(type)) continue;
    const h = hay(e), req = !!(e.required || e.getAttribute('aria-required') === 'true');
    if (type === 'password') { add(map.password ? 'confirm' : 'password', e, true); continue; }
    if (type === 'checkbox') {
      if (/newsletter|marketing|promo|offers|updates|subscribe|reklam|oferty|powiadomien/.test(h)) continue;
      if (/terms|privacy|policy|conditions|agree|accept|regulamin|polityk|akceptuj|zgadzam/.test(h)) terms.push(e);
      else if (req) others.push(labelOf(e) || e.name || 'a checkbox');
      continue;
    }
    if (type === 'radio' || e.tagName === 'SELECT' || e.tagName === 'TEXTAREA') {
      if (req) others.push(labelOf(e) || e.name || e.tagName.toLowerCase());
      continue;
    }
    if (type === 'email' || /e-?mail/.test(h)) { if (!add('email', e, req)) add('email_confirm', e, req); }
    else if (ac(e) === 'username' || /user ?name|login|nick|handle|nazwa uż/.test(h)) add('username', e, req);
    else if (/first ?name|given|imi[eę]\b/.test(h)) add('first_name', e, req);
    else if (/last ?name|surname|family|nazwisko/.test(h)) add('last_name', e, req);
    else if (/full ?name|your name|imi[eę] i nazwisko|^\s*name\s*$|\bname\b/.test(h)) add('full_name', e, req);
    else if (req) others.push(labelOf(e) || e.name || type || 'a field');
  }
  const SIGNUPISH = /sign ?up|register|create|join|continue|next|zarejestruj|załóż|utwórz|dalej/i;
  const text = e => (e.innerText || e.value || e.getAttribute('aria-label') || '').trim();
  const buttons = [...scope.querySelectorAll('button,input[type="submit"],[role="button"]')].filter(visible);
  const submit = [...scope.querySelectorAll('button[type="submit"],input[type="submit"]')].filter(visible)[0] ||
    buttons.filter(b => P.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING).find(b => SIGNUPISH.test(text(b))) || null;
  window.__fleetSignup = {map, terms, submit};
  const bare = h => h.replace(/^www\./, '');
  let sendsTo = '';
  try { if (P.form && P.form.getAttribute('action')) sendsTo = new URL(P.form.action, location.href).hostname; } catch (e) {}
  const a = bare(sendsTo), b = bare(location.hostname);
  const crossSite = !!a && a !== b && !a.endsWith('.' + b) && !b.endsWith('.' + a);
  return {kind: 'register', roles, required, terms: terms.map(labelOf), others, submit: !!submit,
          submitLabel: submit ? text(submit).slice(0, 40) : '', crossSite, sendsTo};
})()"""

FOCUS_JS = r"""((role) => { const c = window.__fleetSignup; const el = c && c.map[role];
  if (!el || !el.isConnected) return false;
  el.scrollIntoView({block: 'center'}); el.focus(); if (typeof el.select === 'function') el.select();
  return document.activeElement === el; })(__ROLE__)"""

TERMS_JS = r"""(() => { const c = window.__fleetSignup; if (!c) return false;
  for (const el of c.terms) if (el.isConnected && !el.checked) el.click();
  return c.terms.every(el => el.checked); })()"""

SUBMIT_JS = r"""(() => { const c = window.__fleetSignup; if (!c || !c.submit || !c.submit.isConnected) return false;
  c.submit.click(); return true; })()"""

PAGE_TEXT_JS = "document.body ? document.body.innerText.slice(0, 4000) : ''"


@dataclass
class FillResult:
    accepted: bool  # the sign-up form is gone afterwards
    submitted: bool = False  # the form was sent (so an account may exist even when `accepted` is False)
    needs_verification: bool = False  # the page says an email must be confirmed
    typed: list[str] = field(default_factory=list)


async def plan(send) -> dict:
    """{'kind': 'register' | 'change' | 'none', roles, required, terms, others, submit}. Failure counts as 'none'."""
    try:
        return await _evaluate(send, PLAN_JS) or {"kind": "none"}
    except Exception as e:  # the page may be navigating
        log.debug("sign-up check skipped: %s", e)
        return {"kind": "none"}


async def _focus(send, role: str) -> bool:
    return bool(await _evaluate(send, FOCUS_JS.replace("__ROLE__", json.dumps(role))))


async def fill(send, values: dict[str, str], *, settle_seconds: float = 5.0) -> FillResult:
    """Type `values` into the sign-up form, tick the terms boxes, submit once. Refuses a form it doesn't fully
    understand (anything other than a sign-up form, or a required field Fleet has no value for)."""
    if (await classify(send)).get("kind") != "register":  # a login form is never ours to fill
        return FillResult(False)
    info = await plan(send)
    if info.get("kind") != "register" or info.get("others") or info.get("crossSite"):
        return FillResult(False)  # a form that posts the details to another site is never filled
    typed: list[str] = []
    for role in FILL_ORDER:
        if role not in info["roles"]:
            continue
        value = values.get(role)
        if not value:
            if info["required"].get(role):
                return FillResult(False, typed=typed)  # a required field we have nothing honest for
            continue
        if not await _focus(send, role):
            return FillResult(False, typed=typed)
        await send("Input.insertText", {"text": value})
        typed.append(role)
    if info["terms"] and not await _evaluate(send, TERMS_JS):
        return FillResult(False, typed=typed)
    if not (info.get("submit") and await _evaluate(send, SUBMIT_JS)):
        if "password" in typed and await _focus(send, "confirm" if "confirm" in typed else "password"):
            await _press_enter(send)
        else:
            return FillResult(False, typed=typed)
    submitted = True
    deadline = asyncio.get_running_loop().time() + settle_seconds
    while asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.3)
        if (await plan(send)).get("kind") != "register":
            try:
                text = str(await _evaluate(send, PAGE_TEXT_JS) or "")
            except Exception:
                text = ""
            return FillResult(True, True, bool(VERIFY_RE.search(text)), typed)
    return FillResult(False, submitted, typed=typed)
