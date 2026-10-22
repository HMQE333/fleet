# Fleet — Operating Rules

Fleet automates work in browsers and on the web for the person running it.
These rules define what the product does and doesn't do. Most of them are
enforced in code; the "Enforced by" line says where. If you change a rule,
change the code that enforces it in the same commit.

---

## 1. Real accounts only

Every browser profile connected to Fleet (an AdsPower profile, or a local Chrome profile Fleet runs itself)
belongs to one real account that the operator owns or is authorized to use (their own account, their company's
account, a client's account under contract).

Fleet does not:
- create accounts in bulk or generate identities to sign up with (see below for the one thing it does)
- run fake personas, sockpuppets, or accounts that pose as independent people
- produce fake engagement: likes, follows, votes, reviews, or comments meant to
  look like they come from unrelated people
- help an account get around a ban or suspension

**Signing up for the operator.** When a task needs an account on a site, Fleet can create ONE for the
operator, under these conditions, all enforced in code:
- the task was started with "Allow Fleet to create an account" (off by default, never inherited by
  scheduled runs);
- the details typed are the operator's own (Settings → Your details for sign-ups: name, email, username),
  never invented ones; Fleet needs at least their email;
- every sign-up is approved in the Inbox. There is no automatic mode. The card says what will be filled and
  which terms boxes get ticked; "I'll do it myself" and "Stop" are always there;
- at most one account per site and profile (an existing saved login means no second account), and at most
  `registrations_per_day` (default 3) accounts a day across all tasks;
- only forms Fleet fully understands are filled: a required field it has no honest value for (phone, address,
  birth date, a dropdown...) means the operator signs up. Terms/privacy boxes are ticked as the card said;
  newsletter and marketing boxes never are. Change-password and login forms are never treated as sign-ups,
  and a form that posts to a different site than the page is never filled;
- the password is saved before the form is sent. If the form was sent but the page still shows it, Fleet
  can't tell whether the account exists, so it keeps the password (labelled "unconfirmed") instead of
  losing a valid one; it deletes it only when nothing was submitted;
- sites the operator listed (Settings → Your details → Never sign up on these sites, subdomains included) are refused before the operator is
  even asked;
- email confirmation and captchas stay with the operator ("Confirm your email" card).

Enforced by: profiles must be explicitly enabled in the panel (off by default);
the planner and browser agents are instructed to refuse persona/fake-engagement tasks and, unless the
task allows registration, not to create accounts (`fleet/prompts.py`); `fleet/registration.py`,
`fleet/identity.py`, `fleet/fastbrowser/register.py`, `tests/test_registration.py`. No bulk or persona
features exist.

## 2. Humans handle challenges

A captcha, "verify you're human" page, 2FA prompt, or identity check **pauses
the subtask** and asks the operator to finish it in the AdsPower window. Fleet
never tries to solve or bypass one.

Enforced by: the captcha detector that runs after every browser step
(`fleet/guard.py`); `request_human` tool; browser-use's `captcha_solver` is
never enabled; Scrapling's `solve_cloudflare` is never used.

## 3. AI models never see passwords

By default the operator logs in by hand in the AdsPower profile and Fleet reuses the session. The
operator may also save a login for a site (Settings → Site logins). When a task reaches that site's
login form, Fleet's **code** types the saved username and password. The AI model never receives them:
they are not in prompts, events, traces, logs or API responses.

- Each saved login asks in the Inbox before it is used, unless the operator marked that login
  "automatic". The operator can always answer "I'll do it myself".
- One automatic attempt per run and site: a wrong password never turns into a lockout loop; Fleet
  hands over to the operator instead.
- A saved login is only ever typed into a login form, never into a sign-up or change-password form.
- Two-step logins (a username page, then a password page) work the same way, with extra care because the
  username page has no password field to recognise it by: Fleet only looks when the operator saved a login for
  that site, and only treats the page as a username step when it has exactly one identifier field and nothing else
  to fill in, talks about signing in, and doesn't talk about signing up, subscribing or a newsletter. A
  newsletter box, a contact form, a comment form or a sign-up page never gets the saved username.
- Fleet never types 2FA codes, card numbers or government IDs.
- Passwords the operator typed in are write-only. The one exception: the password Fleet **generated** for an
  account it registered for the operator. It is stored encrypted before the form is submitted (and removed
  again if the site refused), the operator can show it in Settings (hidden again after 30 seconds, never
  cached, each reveal is logged without the value), and no model, prompt, event or trace ever holds it.

Enforced by: `fleet/site_accounts.py`, `fleet/saved_login.py`, `fleet/fastbrowser/login.py`,
`fleet/registration.py`, `tests/test_site_logins.py`, `tests/test_registration.py`.

## 4. Irreversible actions need approval

Before the agent does something that can't be undone, it asks the operator and
waits. This covers: payments and purchases, sending messages or emails to
other people, publishing or posting, deleting data, accepting terms or
granting permissions, and changing account settings.

Payments and purchases always need approval. For everything else, a task can
be started with approval turned off when the operator has decided that kind
of action is routine for that task (e.g. "post these 5 drafts to my own page").

**Trust rules.** When the Inbox asks to approve a click in the fast browser, the operator may answer
"approve and always allow". Fleet then clicks that exact button without asking next time, and logs every use.
A task can opt out of all rules (every click that needs approval then asks). A rule is as narrow as it can be: the exact label, the exact site (a subdomain is another site), one profile,
created only from a card the operator answered and removable any time in Settings → Trust rules. Payments,
deleting, cancelling, accepting terms, signing up, bookings and generic labels ("OK", "Confirm", "Submit") can
never be trusted, whatever a rule or a client says. Trust a rule only on a site whose content you trust: it
recognises the button by its label. Captchas, logins, sign-ups and the full browser agent's free-text approvals
(its own `request_approval` tool) are not covered by rules; its clicks are (see below).

**Answering together.** The Inbox groups waiting cards by task. For a group, the operator may approve the cards that a
click would approve anyway (a saved login, an approval that is explicitly not a payment) or stop them all, in one action
on exactly the cards they were shown. Payments, sign-ups, captchas, take-overs and "I've handled it" answers are never
accepted in bulk, and an approval whose payment status is unknown is treated like a payment. Each card is logged like a
single answer.

**Retrying a step** is the operator's own action on a finished task, and is refused for a step that already did something that
can't be undone, so retrying can't repeat a payment, a post or a sent message.

Enforced by code in both browsers, not by the model's good manners: `fleet/click_approval.py` decides for every click whose
label reads like a commitment (`fleet/fastbrowser/policy.py`, English and Polish, leaning towards asking). The fast browser asks
before its click; the full agent's `click` action is wrapped (`FleetTools` in `fleet/executors/browser.py`), also for clicks by
position. A real run showed why: asked to "place the order", the full agent clicked "Submit order" without calling its own
`request_approval` tool. That tool, the task's `require_approval` flag, the agent instructions, `fleet/trust.py` and
`policy.can_trust` remain (`tests/test_trust.py`, `tests/test_fastbrowser_unit.py`). A step that already did an approved action is
never redone automatically.

## 5. One profile, one worker, one fixed identity

A profile is driven by one worker at a time. Its proxy is set once in AdsPower
and never changes while the account is logged in. Fleet has no proxy rotation
feature. A local profile (`fleet/browsers.py`) is one Chrome or Edge user data directory under `data/profiles`,
started by Fleet with no proxy and no fingerprint tricks: it is the operator's own browser, nothing more.

Enforced by: profile leases in Postgres (`fleet/profiles.py`); one process per local profile per worker (`fleet/browsers.py`).

## 6. Be a polite visitor

- Public-page fetches honor `robots.txt` and a minimum delay per domain.
- Tasks can be limited to an allowlist of domains (whole domains: `evil-example.com` is not `example.com`);
  profiles can be too.
- Fleet stops a subtask that keeps failing instead of retrying without limit.
- No worker (fetch, scrape or browser) ever visits this machine or the local network (Fleet's API,
  AdsPower's Local API, routers, cloud metadata), so a hostile page or plan can't steer them there.
  Page reads check the address after DNS and again after redirects.
- A crawl step reads one section of one public site (the start URL's own folder, up to `crawl_max_pages` = 50 pages
  and `crawl_max_depth` = 3 clicks, files and nofollow links skipped, endless query variants cut off), through the
  same fetch as everything else (robots.txt, pacing, public addresses), may read the site's sitemap.xml the same way (never with
  an XML parser, so entities and external references can't be used), and stops after three refusals in a row.
- The operator can raise or lower the caps on pages, depth, items, time and accounts per day in Settings → Limits, each within
  a ceiling set in code (`fleet/limits.py`). Politeness (the delay between requests to one site, robots.txt) and the
  public-address checks cannot be changed from the panel.
- A scrape step that opens each item's own page (`scrape_max_details`, 30 by default, per step) follows only links
  on the list's own site, through the same checks and pacing as every other page read, and stops after three
  refusals instead of pushing on.

Enforced by: `fleet/webfetch.py` (public addresses, allowlist, robots, pacing), browser-use
`allowed_domains` / `prohibited_domains` / `block_ip_addresses`, `fleet/fastbrowser/netguard.py`
(public addresses only, checked after DNS), `tests/test_webfetch.py`, `max_attempts` / `max_steps` limits.

## 7. Everything is logged; everything can be stopped

Every plan, step, screenshot, human request, and result is written to the
`events` table and shown in the panel. Cancelling a task stops all of its
workers at their next step.

The operator can also **pause** a running task (steps already running finish, nothing new starts until they
resume) and **take over** a running browser step: the worker waits at its next step, nothing is clicked while the
operator uses the window, and Fleet carries on from whatever the page shows when they give control back (or stops
the step if they say so). Only the operator's own request does either.

Every browser step also leaves a record in the **experience base** (`experience` table): the site, the step's
instructions, the path it took (element labels and typed values; never a password, card or code, which the page reader
does not offer), how it ended and why in one line, the cost and the time. Never page text, never a model's prose. It is
what lets Fleet know a site before it goes back; the operator can read and prune it.

Enforced by: `fleet/events.py`, `fleet/experience.py` (called by both browser workers after every run, whatever the
outcome), the stop callback in the browser executor, `tasks.paused` in the worker's claim queries,
`hitl.take_over_requested` in both browser workers (`tests/test_control.py`, `tests/test_experience.py`).

## 7b. What is kept, and what can be cleaned up

By default Fleet keeps everything. If the operator sets a retention period (Settings → Limits), finished tasks older than that lose only
their screenshots, saved agent histories and plain step / log lines. Running work, answers, scraped data, saved automations and every audit
event (approvals, Inbox answers, trust rules, sign-ups, password reveals) are never cleaned up. Enforced by: `fleet/retention.py`
(`tests/test_retention.py`).

## 8. API keys are locked away

Keys pasted in Settings are encrypted (AES-256-GCM) before they reach the database. The master key
lives in the OS credential store (or `FLEET_MASTER_KEY` on a server), never in the database. The API
can save, replace, check or delete a key but never return it, and any key Fleet has used is scrubbed
from events and logs.

Enforced by: `fleet/vault.py`, `tests/test_vault.py`.

## 9. Data stays with the operator

Results, screenshots, and history files are stored locally under `data/`.
Page content is sent only to the model API the chosen model names — OpenRouter (and the provider it routes
to), or Anthropic directly for an `anthropic-api/…` model (Settings → Models). Nothing else: no third-party
analytics (browser-use telemetry is disabled), and no other API is ever called with the text of a page.
Which API a model id goes to is decided in one place, `fleet/providers.py` (`tests/test_providers.py`), and the
key for it is read from the vault, never from a prompt.

The one other thing that can leave this machine is OPT-IN: if the operator enters a webhook address (Settings → Notifications), Fleet sends
that address one short line per event the operator ticked (something needs you, changes were found, a task finished). Never a password or
key (scrubbed), never more than that line, never to an address that isn't a public https one (checked again when sending, redirects not
followed), at most 20 an hour. The address itself is stored encrypted and never shown again. Enforced by: `fleet/notify.py`
(`tests/test_notify.py`).
