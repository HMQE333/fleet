# Autonomous progress log

Short notes so later passes don't rediscover the same things. Newest first.

## Pass 1 — local API security (2026-09-22)
**Found**
- Path traversal: the panel's catch-all route did `WEB_DIST / path` unchecked; `GET /..%2F..%2Fpyproject.toml`
  returned the file. `.env` (API keys) was readable by anything that could reach the port.
- DNS rebinding: the API answered any `Host`, so a hostile page could become "same-origin" and read tasks /
  screenshots or create tasks with approval off.
- Body-less POSTs (`/profiles/sync`, `/tasks/{id}/cancel`, `/profiles/{id}/open`) were CSRF-able.
- Not vulnerable: `text/plain` JSON CSRF (FastAPI refuses non-JSON bodies); `/files` (StaticFiles checks paths).

**Changed**: panel served by `StaticFiles(html=True)` (hash routes need no fallback); `TrustedHostMiddleware`
(localhost + `FLEET_ALLOWED_HOSTS`); unsafe methods need `X-Fleet-Client` and a local/absent `Origin`.
Panel sends the header.

**Verified**: `tests/test_api_security.py` (5 attack tests) + live curl probes (404/400/403, panel 200/201).

**Also**: test DB is now dropped/recreated each run (an edited migration had left a stale schema that produced
11 false failures). Rule: never edit an applied migration — add a new one.

## Pass 2 — OpenRouter + encrypted key vault (user request; product couldn't run without a Claude key)
**Changed**
- `openrouter.py`: one client for all models; retries only on 408/429/5xx/timeouts; 401/402/404 fail fast
  with advice; real cost from `usage.cost`; `<think>` scratchpads stripped; catalog cached 1h.
- `models.py`: 7 roles (planner, smart, fast, browser, research, deep_research, video), defaults in code,
  overrides in `settings`; validated against the live catalog (capabilities: structured_outputs, image, video).
- Structured output: strict JSON schema + `provider.require_parameters`; Pydantic validation + one repair
  round; schema-in-prompt fallback for models without structured outputs.
- New `video` worker (YouTube → Gemini on `google-ai-studio`; refused before spending otherwise).
- Research: Perplexity natively, any other model via OpenRouter's `web` plugin; sources appended.
- `vault.py`: AES-256-GCM, name as AAD, master key in Windows Credential Manager / `FLEET_MASTER_KEY`;
  write-only API; redaction of used keys in events. AdsPower key moved into the vault.
- Panel: Settings page (keys + models), first-run banner, Run disabled until a key exists.

**Verified**: 40 tests (fake OpenRouter: formats, repair, retries, 401/402/404, sources, video routing,
vault tamper/AAD, write-only API, redaction). Live: real catalog (444 models), real `/key` check rejected a
fake key with a readable message, model validation refused unfit/unknown models.

**Not verified**: a real paid model call (no OpenRouter key on this machine).

## Pass 3 — permission boundary + no double actions
**Found**: plans ran immediately, so the AI's choice of which logged-in profile to use stood in for the
operator's authorization; a crash after an approved irreversible action would re-run the subtask (action
twice); a double click could answer a human request twice; `_promote` queued work for tasks in any status.

**Changed**
- Plan review (deterministic rule, not AI): `plan_review` auto|always|never per task; auto = ask when a round
  contains browser subtasks. New status `awaiting_approval`; `/tasks/{id}/approve` (conditional → 409 twice).
  Review card on the task page, plans in the Inbox, CLI prints the plan and asks (`--yes` to skip).
- `subtasks.side_effects` set when an irreversible action is approved (or auto-approved); worker retry and
  the reaper then fail instead of requeueing, with an explanation.
- `hitl.respond` only answers once; `_promote` only for running tasks.

**Verified**: 44 tests (+4: waits for OK and nothing starts meanwhile; read-only plans auto-run; always;
side-effect subtask not retried; double answer refused). Live: inbox → review → run → 409 on second approve.

## Pass 4 — unnamed browser work stays off account profiles
**Found**: a browser subtask without a named profile leased *any* enabled profile, e.g. a personal Gmail
profile, for unrelated browsing — a prompt-injection path into that login.
**Changed**: `profiles.general_use` (off by default). Unnamed subtasks lease only general-use profiles; named
ones only the named profile. Clear failure when none qualifies. Planner sees the flag; Profiles page toggle.
**Verified**: 46 tests (+2: unnamed work lands on the general profile while the named one uses the account
profile; clear failure without a general profile). Live toggle saves.

## Pass 5 — recovery: "Run again"
**Found**: a failed task (no credits, site down, bad plan) could only be retried by retyping the prompt.
**Changed**: `POST /tasks/{id}/rerun` copies prompt + settings of a finished task (409 otherwise); "Run again"
button on finished tasks; failure card tells what to fix first.
**Verified**: 47 tests (+1 rerun: 409 while queued, copy keeps budget/plan_review).

## Pass 6 — saved automations + schedules (the missing core of "automate")
**Found**: only one-off tasks; every repeat meant re-typing and re-planning (new AI decisions each time).
**Design**: an automation = the *done* steps of a finished task (already seen/approved) + settings + optional
schedule. A run is a normal task with those subtasks pre-inserted: planner skipped, `max_rounds=1` (review
only writes the answer), `plan_review=never` (steps were approved when saved). Schedules are plain code
(`schedules.py`): daily at HH:MM on ISO weekdays, or every N hours; IANA time zone (needs `tzdata` on Windows).
`run_due` claims with `FOR UPDATE SKIP LOCKED` and advances `next_run_at` first → one run per slot even with
several conductors; missed slots aren't replayed; runs never overlap (409 / skipped event). Events are emitted
after commit (they reference the new task). Deleted profile → that step fails with a clear message.
**UI**: "Save as automation" on finished tasks, schedule editor, Automations page (enable, run now, change
schedule, approval toggle, steps, last run).
**Verified**: 56 tests (+9: weekday/weekend, same-day slot, DST change in Warsaw, hourly, validation, save keeps
only working steps, replay without planner, no overlap, two conductors → one run, deleted profile). Live:
save with schedule → "Weekdays at 07:30 · next in 18 h" → Run now → second Run now refused; toggle saves.

## Pass 7 — access token actually usable
**Found**: with `FLEET_API_TOKEN` set (the documented step before exposing Fleet) the panel had no way to enter
the token → every call 401, product unusable. Screenshots (`/files`, pictures of logged-in pages) weren't
covered by the token at all.
**Changed**: token screen ("Fleet is locked") on 401, stored in this browser; `/files` requires the token when
set (img URLs carry `?token=`); one `_token_ok` check shared by API and files.
**Verified**: 57 tests (+1). Live with a token: locked → wrong token stays locked → right token unlocks, live
updates connect; screenshot without token → 401.

## Periodic architecture review (after pass 6)
- Shared test fixtures moved from `test_pipeline.py` to `conftest.py`; `ruff` clean (E,F,W,B,UP,SIM).
- Left as is on purpose: two 4-line cost-recording queries (worker/conductor) — not worth an abstraction.
- Parked in FUTURE_IDEAS.md: retention, pending-subtask index, tenants, triggers, recipe replay.

## Pass 8 — first-run database errors
**Found**: a stopped Postgres or a bad `FLEET_DATABASE_URL` ended `fleet serve` in a raw asyncpg traceback.
Also found (not changed, reported to the operator): the local Postgres 17 accepts connections without checking
passwords (pg_hba `trust`), and my check created an empty `fleet` database there — the one Fleet uses anyway.
**Changed**: `DatabaseUnavailable` with host:port/db (never the password) and what to check; `serve` checks the
database before starting the web server; the CLI prints the message and exits 1.
**Verified**: unreachable port → one readable line for `serve` and `migrate`; 57 tests, ruff, build.

## Fast browser (browser_fast) — from jev-ultrafast + JevBrowser (both MIT)
**Built**: `fleet/fastbrowser/` — async CDP client, page reader (adapted snapshot.js: no password / card /
OTP fields, link hrefs, login flag), page actions with freshness + hit tests, Jev via OpenRouter
`/api/alpha/decisions` (one key), noise filtering, `back`, goal/stuck watchers, next-best recovery, public-only
network guard, label-based approval policy (EN/PL), recorded-path following. Executor `browser_fast` with
Fleet hooks (captcha/login → Inbox, approvals, missing values asked in the Inbox, screenshots, cancel) and
hand-over to the full agent when stuck (never after an approved action). Full-agent runs now record a
replayable path; automations replay saved browser steps as browser_fast with that path. Settings switch
(on by default). Full agent now also blocks local/private hosts and raw IPs.
**Verified**: 111 tests — engine against real headless Edge (type, click, finish, replay with zero model calls,
approval refused/approved, login hand-off, private address refused), Jev request shape + costing, policy,
network guard, hand-over / no-redo / switch / learned-once-replayed-fast, and one end-to-end run of the real
worker through Fleet with an Inbox approval. Panel checked live.
**Not verified**: a real Jev call (needs an OpenRouter key; endpoint confirmed to exist: 401 vs 404).

## Cost control (after a real $2.36 run)
**Found** (real data): 87% of the cost was two Perplexity deep-research steps ($1.15 + $0.91) the planner chose
for quick follow-ups — the "only when asked" rule was prompt-only. The $2 budget overshot because both
started while spend was ~$0.30 (budget checked spend, not expected cost). Round 2 existed because shops'
robots.txt blocks their search pages. The final review then hit "out of credits" and the operator got nothing.
OpenRouter's key usage ($2.60) matched Fleet's recorded costs exactly.
**Changed**: deep research opt-in per task (deterministic downgrade + visible warning); `costs.py` expected cost
per step; claims reserve running steps' expected costs + $0.15 for the review; steps that can never fit are
skipped with numbers; estimate stored, shown on plan review / task page; plans ≥ $1 wait for OK (auto mode);
research answers capped (~400 words, 4k tokens); review reads 6k chars per result (was 20k); page reads drop
nav/footer/aside/site header and cap at 40k chars; planner told prices, robots/search-page tip; failed review
keeps the workers' results.
**Verified**: 121 tests (+5 replaying the real run's failures), lint, build.

## Loop run 1 — saved logins (S2a in ROADMAP_AUTOMATION.md)
**Why**: tasks behind a login could only continue if the operator was logged in already or did it by hand
every time the session expired. The operator asked for logins that Fleet can use, while keeping control of the Inbox.
**Built**: per-site (optionally per-profile) saved logins, AES-256-GCM bound to their row; page-side detection
that only treats a form as login when it is one (sign-up, confirm-password and change-password forms are never
filled); code types the login via CDP (the model never sees it); the Inbox asks first unless that login is marked
automatic; "I'll do it myself" always works; one attempt per run and site, then a hand-off card that says it
didn't work; both browser workers; Settings → Site logins; write-only API; 422 errors no longer echo rejected input.
RULES.md §3 rewritten to match (it used to say Fleet never handles passwords).
**Verified**: 139 tests (18 new, real headless Edge on local login/sign-up/change-password/bare pages, full worker
path with the Inbox), ruff, build, live panel check. Mutation checks: removing sign-up detection fails 3 tests;
writing the password into an event is caught (and Fleet's scrubber had already replaced it).
**Not verified**: a real site's login (no real credentials used, by design).

## Loop run 2 — safe fetching + structured scraping (S1)
**Found first (security)**: the fetch worker only checked robots.txt, so a plan step could make it read
`http://127.0.0.1:8710/api/...` (Fleet's own API), AdsPower's Local API, LAN devices or cloud metadata and hand the
content to a model — confirmed against the running server. Also the allowlist used "ends with", so `evil-example.com`
passed `example.com`. Fixed in its own commit: `fleet/webfetch.py` (public addresses only, after DNS and again on the
final URL; whole-domain allowlist; robots; pacing), RULES.md §6, 11 tests (mutation-checked).
**Built (S1)**: `scrape` worker. A model learns a recipe once (CSS selectors for items/fields/next link, shown a trimmed
digest + pagination hints from the untrimmed page); code validates it against the real page (several items, mostly-filled
fields; one repair round), extracts every page, follows same-site next links, de-dupes, stops at caps (10 pages / 300
items / 300 s). Partial data is kept when a later page is blocked. JS shells are detected before spending anything.
Results in `scrape_results` (+ recipe); automations carry the recipe, so scheduled scrapes cost $0 until the layout
changes. API + panel: preview table, CSV (BOM, formula-neutralised) and JSON downloads, behind the access token.
**Verified**: 169 tests (+31), ruff, build, live panel on real data; mutation checks (formula neutralisation, same-site
rule, SSRF guard). Real run: books.toscrape.com, 200 items / 10 pages, $0.0215; second run with the saved recipe $0.
Found and fixed a layout bug (data table made the page scroll sideways on narrow screens).
**Not verified**: sites that need cookies/consent walls, or aggressive anti-bot (by design those go to a browser step).

## Loop run 3 — registration (S3)
**Why**: a task that needs an account used to stop at the sign-up form and wait for the operator every time. The
operator wants Fleet to register when a task requires it, while still controlling every Inbox item.
**Built**: task option "Allow Fleet to create an account" (off by default; scheduled runs never inherit it);
Settings → Your details for sign-ups (their own name/email/username, validated); a page-side form reader that tells a
sign-up form from login and change-password forms, maps fields, lists required fields Fleet has no honest value for
(then the operator signs up), and collects only terms/privacy boxes (newsletter/marketing are never ticked); code types
the values and submits once. Every sign-up is approved in the Inbox (`register` card: what is filled, which terms get
ticked; "I'll do it myself" and "Stop" available); one account per site and profile, 3 a day; generated 20-char
password saved encrypted BEFORE the submit and removed again if the site refused; `Registered by Fleet` pill and
a logged, no-store "Show password" (Fleet-created accounts only); email confirmation goes to the operator
(`verify_email` card); the step is marked as having side effects. Both browser workers; planner and agent prompts say what is allowed.
**Review fixes before commit**: a sign-up form that posts to another site is never filled; the daily limit and
existing-login checks run again after the operator approves (parallel steps); a form that was sent but still shows
is kept as "unconfirmed" with its password instead of being deleted (the account may exist); a create() race no
longer crashes the step.
**Verified**: 202 tests (+33: real headless Edge on sign-up/phone/change-password/login pages, full worker path with the
Inbox, API and reveal rules), ruff, build, live panel (details form, composer option, both Inbox cards, reveal/hide).
Mutation checks: allow flag, one-per-site, daily limit, phantom-account cleanup, operator choice, unknown required
fields, sign-up classification, change-password, reveal origin, side-effects flag all fail a test when removed.
(The newsletter rule is protected twice; it takes both layers removed to tick the box, and that is caught too.)
**Not verified**: a real site's sign-up (no real identity used, by design); sites with captchas, multi-page or
email-code sign-ups are handed to the operator (S7).

## Loop run 4 — trust rules (S4, slice 1)
**Why**: the operator wants more automation but control over every Inbox item. A daily automation that sends the same
message re-asked the same approval every run. Now the operator can say "approve and always allow" once, and still
revoke it any time; everything new keeps asking.
**Built**: the approval card for a fast-browser click carries an offer; answering "Approve and always allow “X” on
site" saves a rule (exact normalised label, exact host without www, this profile only). Next time the same click is
approved by the rule: logged ("Approved by your trust rule…", counted, last-used time), the step is still marked as
having acted (no automatic retry), and automations/scheduled runs benefit too. Never trustable (code, not config):
payments, deleting/removing/cancelling, unsubscribing, transfers, donations, bookings, accepting terms, signing up,
generic labels (OK, Confirm, Yes, Submit, Continue...), Polish equivalents. Rules cannot be created through the API (only
via an answered card; POST is 405), a rule planted straight in the database for a payment is ignored at use time,
`remember` is only honoured for approvals the card offered it for. Settings → Trust rules lists and removes them.
**Verified**: 249 tests (+47: label policy table, exact-match store, API, full worker path: remember, reuse without a
card, plain approval remembers nothing, reject never creates a rule, payments never offered/remembered, other profile
doesn't match), ruff, build, live panel (card shows the button only when offered, request body has remember:true,
no overflow at 480px, list + delete). Mutation checks: profile scope, subdomain match, can_trust at find/add, remember
on reject, remember without an offer, offer for every click, logging, side-effects mark, payment and generic-label
rules — all fail a test when removed (two were first missed, tests strengthened).
**Not verified**: a real site. Rules apply to the fast browser only; the full agent's free-text approvals still ask.
**Next**: S4 slice 2 (pause/resume/take over a running task, Inbox grouping by task) or S5 crawl worker.

## Loop run 5 - change monitoring (S6, slice 1)
**Why**: scraping once is a snapshot; most real uses (prices, new listings, stock) want "what changed since last time".
Scheduled automations already re-read the pages for $0, so the comparison is free too.
**Built**: `fleet/changes.py` (pure code, no model): rows are matched by a column that is filled and unique in both runs
(ids and links preferred over names, so a renamed product is an edit); added / removed / edited rows with per-field
before and after, exact counts with capped lists; whitespace differences ignored; no "new / removed" claims when either
run stopped early (limits or a blocked page: a row that wasn't reached isn't a removed row); different columns are
reported as not comparable; without a unique column an edit shows as one removed plus one added row. The scrape worker
compares with the previous run of the same step of the same automation (the first automation run compares with the task it
was saved from), stores the result in `scrape_results.changes`, puts a short report above the usual summary and logs a
`scrape.changes` event. Panel: "N new / N removed / N changed" pill on the step and the lists in the preview.
**Found while testing**: the shop fixture repeats a product on two pages, so a real site's duplicate links make the id
column unusable and the diff falls back to whole rows; kept as designed and covered by a test.
**Verified**: 264 tests (+15), ruff, build, live panel with seeded results (lists, colours, no overflow at 420px).
Mutation checks: key preference, duplicate/empty key cells, partial handling (logic and worker), whitespace, column
mismatch, multiset matching, source-task baseline, latest-run baseline, saving the result - all fail a test when removed.
**Not verified**: a real site over several days. Needs no new settings; no real model calls this run ($0).
**Next**: S6 alerts (only report when something changed; email or webhook), S4 slice 2 (pause/take over), S5 crawl.

## Loop run 6 - two-step logins (S7, slice 1)
**Why**: most big providers and many shops ask for the email on one page and the password on the next. The saved-login
code only recognised a page with a password field, so these always fell back to "log in by hand", the main gap in "log in
when the task needs it".
**Built**: `login.classify_step` (page script) decides whether a page is the username step of a login, `perform_step` types
the username and continues, `perform_two_step` follows with the password page through the existing `perform`. Strict by
design, because there is no password field to be sure with: Fleet only looks when a login is saved for the site
(`SavedLogin.watching`), and the page must have exactly one identifier field and nothing else to fill in, talk about signing
in (heading, title, address or button) and not about signing up, subscribing or a newsletter. Both browser workers; the
Inbox card (`login_saved`), automatic mode, "I'll do it myself", one attempt per run and the wrong-password hand-off card are
unchanged. Log line says "in two steps".
**Found by reviewing my own script**: the field filter treated "comment" and "message" boxes like search boxes and ignored
them, so a "Sign in to comment" form (email + comment box) would have counted as a username step. Fixed and covered.
**Verified**: 295 tests (+31: real headless Edge on a page that swaps its form, a two-page flow, wrong password, unknown
username, newsletter / contact / comment / sign-up pages, the whole worker with the Inbox and automatic mode, nothing in the
database), ruff. Mutation checks: login-context rule, other-fields rule, one-page code used for two steps, watching, sign-up
guard, no-account guard all fail a test when removed (two checks are redundant defence in depth: one identifier / step guard).
**Not verified**: a real provider's login (no real credentials, by design); iframe logins; a third step. Backend-only change, so
the panel was not touched and needs no live check.
**Next**: S6 alerts, S4 slice 2 (pause / take over), S5 crawl worker, iframe logins.

## Loop run 7 - pause and take over (S4, slice 2)
**Why**: the operator wants control over everything that runs. Until now the only controls were "approve" and "stop task":
nothing between letting a task run and killing it, and no way to use the AdsPower window mid-step without Fleet clicking
over them.
**Built**: Pause / Resume on a running task (`tasks.paused`: both claim queries skip paused tasks, so steps already running
finish and nothing new starts; event + banner + list pill). Take over on a running browser step (`subtasks.take_over`): the
worker reads and clears the request at its next step (fast engine: `on_page`; full agent: `on_step_end`), asks with a
`takeover` Inbox card ("Give control back" or stop the step) and carries on from whatever the page shows; nothing is
clicked while the operator has the window. Only running browser steps can be taken over; a fresh claim forgets an old
request so a retry doesn't pause unexpectedly. Migration 014.
**Verified**: 303 tests (+8: claim rules incl. a direct claim test, pause/resume end to end, API rules and CSRF, the real fast
browser worker over headless Edge paused mid-run, given back, and stopped), ruff, build, live panel on a seeded running task
(Pause/Resume toggle, banner, list pill, Take over button only on the running browser step, events). Mutation checks:
both claim queries, request clearing, old-request reset, the hook, step-kind and status filters, pause on non-running
tasks, reject handling - all fail a test when removed.
**Not verified**: the full browser agent's `on_step_end` hook (same helper, needs a real browser-use run with model credits);
a real AdsPower window (the worker only waits, so nothing here depends on it).
**Next**: S6 alerts, S5 crawl worker (detail pages), Inbox grouping by task + bulk answers, iframe logins.

## Loop run 8 - item pages in scraping (S5, slice 1)
**Why**: most real scraping needs more than a list shows (descriptions, specs, stock, contact details live on each item's
own page). Until now a scrape step stopped at the list.
**Built**: the list recipe can now name the field holding each item's link and what is wanted from those pages
(`detail_link_field`, `detail_wanted`; ignored when the field doesn't exist). The worker asks the model ONCE how to read one
item page (`DetailRecipe`, shown a real sample, checked by code: at least half the fields must be found, one repair round),
then opens the item pages itself and adds the values as extra columns (names that clash get a `detail_` prefix). Same safety
as every page read: same-site links only, robots.txt, public addresses, pacing, the time cap, cancel; a hard cap per step
(`FLEET_SCRAPE_MAX_DETAILS`, 30); three refusals stop it; list data is kept whatever happens. The recipe (with the item-page
part) is stored, so saved automations reuse it for $0 and relearn it only if it stops fitting. The change report ignores
item-page columns that were only partly read (a lower cap, a failed page) instead of reporting them as edits. No panel change:
the extra columns show up in the table and CSV/JSON as they are.
**Verified**: 317 tests (+14: helpers, a shop with generated item pages through the real worker, cap, automation reuse with no
model call, relearning, repair then keeping the list, refusals, off-site links, missing link field, goals that need nothing
more, change-report caution), ruff. Mutation checks: same-site rule, scheme check, cap, refusal stop, saved-rule check, repair
judge, column clashes, link-field validation, partial/soft-field handling, schema used for the model - all fail a test when removed.
**Not verified**: the model actually deciding to set `detail_link_field` and writing good item-page selectors (the stand-in
returns them; needs OpenRouter credits to judge the prompts), a real shop's item pages.
**Next**: S6 alerts, crawl a whole site section, Inbox grouping + bulk answers, iframe logins.

## Loop run 9 - updates feed (S6, slice 2)
**Why**: change monitoring computed what changed, but the operator had to open each scheduled run to find out. For price
or listing watching the point is to be told when something moved, and to hear nothing when it didn't.
**Built**: `scrape_results.seen` (migration 015). The Inbox endpoint now also returns `updates`: scrapes of saved
automations whose comparison was valid and found at least one new / removed / edited row and haven't been marked as seen,
newest first (cap 50). Quiet runs, plain tasks and tables that couldn't be compared never appear. `POST /api/subtasks/{id}/seen`
and `POST /api/inbox/updates/seen` mark one or all (CSRF header required); saving a result again makes it unseen again.
Panel: an Updates section in the Inbox with counts, a "See what changed" link to the step's data preview and Mark as seen /
Mark all as seen; the Inbox badge counts updates. News, not a question: nothing waits on it, and nothing leaves this machine.
**Verified**: 323 tests (+6: shows and stays quiet, newest first and mark one, mark all, partial and not-comparable cases,
re-save, plain tasks), ruff, build, live panel on seeded runs (card, pills, mark as seen, empty state, no overflow). Mutation
checks: seen filter, comparable filter, quiet runs, mark-one scope, re-save reset, automation join - all fail a test when removed.
**Not verified**: a real scheduled run over days. **Not built on purpose**: email / webhook alerts (outbound destination =
outward-facing, wants its own design and allow rule); price thresholds.
**Next**: crawl a whole site section (map + queue + resume), Inbox grouping by task + bulk answers, iframe logins.

## Loop run 10 - crawl a site section (S5, slice 2)
**Why**: scraping lists and item pages covers shops and directories; documentation, blogs and help centres need the CONTENT of
many pages of one section, found by following links. Doing that with one fetch step per page needs the planner to know every
address in advance.
**Built**: a new worker kind `crawl` (migration 016, planner prompt, costs = $0). `fleet/crawl.py` holds the rules: one
canonical address per page (fragments, tracking parameters and `index.html` dropped, trailing slash kept because it changes how
relative links resolve), the section is the start URL's own folder, files and `rel=nofollow` links are never followed, a trap
guard cuts endless query variants of one path. The worker reads pages breadth-first through the same polite fetch as every
other worker (robots.txt, pacing, public addresses, allowed domains), with hard limits (50 pages, 3 clicks, 300 s), stops after
three refusals in a row and keeps what it has. Progress is saved every 10 pages in the result's `recipe` column, so a step that
is interrupted (restart, lost lease) resumes from its frontier instead of starting over. Results are rows in `scrape_results`
(url, title, depth, words, text), so the data preview, CSV/JSON, change monitoring (new pages, edited text) and the Updates feed
all work for crawls with no extra code. Panel: a Crawl badge; nothing else needed.
**Found while testing**: an early version stripped trailing slashes, which breaks relative links on servers that serve a folder
without redirecting; and Fleet (correctly) refuses redirects to local addresses, so the test site serves folders at both forms.
**Verified**: 347 tests (+24: pure rules; a real crawl through the worker: folder, depth, files, nofollow, robots, off-site links, each
page once, the page / depth limits, trap, refusals, nothing readable, missing start URL, kill and resume without re-reading saved
pages, a saved crawl reporting a new page and edited text and landing in Updates), ruff, build, live panel on a seeded result.
Mutation checks (15): section, site, files, nofollow, tracking params, trap guard, depth, page limit, refusal stop, resume,
progress saving, visited set, incomplete flag, change report, 404 handling - all fail a test when removed (one needed a better
fixture: an off-site link inside the same path).
**Not verified**: a real site section; the planner choosing `crawl` (prompt text only, needs a real model run); sitemap.xml.
**Next**: Inbox grouping by task + bulk answers, iframe logins, price thresholds, sitemap discovery.

## Loop run 11 - Inbox grouping and answering together (S4, slice 3)
**Why**: with several workers in parallel, one task can put five "Click Send" cards in the Inbox; the operator had to answer
them one by one, with nothing connecting them to their task.
**Built**: the Inbox groups waiting cards by task (title, count) and, for a group, offers "Approve N together" and "Stop all".
`POST /api/inbox/respond-many` takes the exact ids the operator was shown and a decision (approve or stop only). Eligible to
approve together: a saved login, or an approval that says it is NOT a payment (cards now carry a `payment` flag set by both
browser workers; one without the flag is treated like a payment). Never in bulk: payments, sign-ups, captchas, take-overs, "I've
handled it" (it would claim the operator did something). Stopping may be done for any kind. Cards that arrived after the
operator looked, or were answered meanwhile, are skipped and reported. Each answer is logged like a single one; approving
together never creates a trust rule.
**Verified**: 370 tests (+23: the eligibility matrix, the API with every kind, only-the-shown-cards, gone/duplicate ids, input checks,
the real fast worker setting the flag on a send and a checkout, and two parallel steps approved in one request through the whole
worker path), ruff, build, live panel (grouped cards, "Approve 2 together" answered two and left the payment card waiting).
Mutation checks: payment / unknown / sign-up approval in bulk, stop restrictions, the waiting check, duplicate ids, the flag in the
fast worker - all fail a test when removed.
**Not verified**: the full browser agent's `payment` flag (same one-line change, needs a real browser-use run; without it its
approvals are simply never approvable in bulk, the safe side).
**Next**: price thresholds on watched scrapes, sitemap.xml discovery for crawls, iframe logins (same-origin only; cross-origin
widgets are deliberately out of scope).

## Loop run 12 - sitemaps for crawls, plus a polish pass
**Why**: a crawl that only follows links misses pages nothing links to, and can't tell how big a section is. Most sites publish
a sitemap. (The operator also asked to keep improving things along the way.)
**Built (crawl)**: after the first page, the crawl reads `/sitemap.xml` (a sitemap index: up to 3 same-site child files) through the
same polite fetch as every page (robots.txt applies: a disallowed sitemap is not read), takes the addresses that are inside the
section (same site, same folder, no files, trap guard), caps them at 500 and queues them one click from the start. Linked pages
still come first. The sitemap text is read with a plain pattern, never an XML parser, so entities and external references can't be
used. A crawl that is only the start page never asks; a resumed crawl keeps the pages it already queued. The summary says how many
pages the sitemap added. A missing, refused or odd sitemap is simply not used.
**Polish**: the README worker table and the hand-off step were missing the crawl, pause, take over, always-allow and bulk answers; the
.env.example lists every limit that can be changed (scrape, crawl, fetch delay, registrations per day); payment cards in the Inbox
carry a visible "Payment" pill; long cells in the data preview show their full text on hover.
**Verified**: 376 tests (+6 sitemap tests (pattern parser with an entity attack, orphan pages found inside the section, index files, robots.txt
disallow, the cap, the one-page crawl, no re-read on resume), ruff, build, live panel (payment pill only on the payment card).
Mutation checks (7): scoping of sitemap pages, off-site index children, the cap, never reading it, reading it again on resume, the
one-page rule, robots.txt - all fail a test when removed.
**Not verified**: a real site's sitemap (gzipped `.xml.gz` sitemaps are skipped on purpose).
**Next**: price thresholds on watched scrapes; same-origin iframe logins; limits editable in the panel instead of .env.

## Loop run 13 - limits editable in the panel
**Why**: the page, depth, item and time caps and the daily account limit lived only in `.env`; changing one meant editing a file and
restarting the server. The operator asked for polish along the way, and this was the most obvious remaining rough edge.
**Built**: `fleet/limits.py` and Settings → Limits. Eight caps (scrape pages / items / item pages / seconds, crawl pages / depth /
seconds, accounts per day), each with a hard range in code; a saved value wins over `.env`, null removes it, everything is validated
before anything is written, a bad stored value is ignored. The scrape worker, the crawl worker and the registration check read
`limits.effective()` per step, so a change applies to the next step with no restart (anything not overridden reads through to the
real settings, so tests and `.env` still work). Not editable on purpose: the delay between requests to one site, robots.txt and the
public-address checks. Panel: a Limits section with the default and the allowed range per limit, a "changed by you" tag, a reset button
and readable errors.
**Verified**: 394 tests (+18: defaults and read-through, saved / null, ranges, unknown and politeness names, bool and fractions, bad stored
values, API incl. CSRF and 422, and the crawl, the scrape and the daily account check really using the saved value), ruff, build, live
panel (change, save, tag, reset, out-of-range error, no overflow). Mutation checks (8): range, bool, unknown names, stored values,
read-through, and each of the three consumers ignoring the panel - all fail a test when removed.
**Next**: price thresholds on watched scrapes; same-origin iframe logins; limits for worker slots (need a restart, so left alone).


## Loop run 14 - check: does redaction survive a restart? (yes; a regression test was added)
Question from the operator: how is password entry by the AI made safe and reliable. While checking, I suspected the in-memory list of
values that `vault.redact` scrubs (events, traces, errors) would be empty after a server restart and only refilled when a password is
saved again, and committed a fix for that (b373c3d). That was wrong: `vault.unseal` already registers every decrypted value, so each time
a saved password is read for a login (or a generated one is shown) it is on the list. The extra calls were removed again and what stays is
a regression test (restart simulated by clearing the list, then a login read / a reveal, then redact scrubs it), mutation-checked on
`vault.unseal`. Passwords are never written to logs by Fleet's own code; redaction is the second layer.

## Loop run 15 - P1: a sweep over every API route
**Why**: ~50 routes now drive logged-in browsers, stored secrets and accounts. The guards (panel header on changes, same-site origin, access token,
host allowlist) were tested on a few routes; a route added later could skip them without any test noticing.
**Built**: `tests/test_api_sweep.py` (7 tests, no database) walks every route the app has: all changes refuse a request without the panel header and
one from another site even with it; with a token set every route (and the /files screenshots) refuses requests with no or a wrong token (header or
`?token=`); everything lives under /api. Found one thing to close: FastAPI's generated /docs, /redoc and /openapi.json pages were served without a token
and listed every endpoint; they are switched off. Every existing route already passed.
**Verified**: 7 tests, ruff. Mutation checks (5): a route without the token dependency, docs back on, header check, origin check, /files token check - all fail a test.

## Loop run 16 - P2: price / threshold watches
**Why**: change monitoring says what moved; for price watching the operator wants "tell me when it drops to X", and only then.
**Built**: `fleet/watches.py` (migration 017: `watch_rules`, `scrape_results.alerts`). A rule is a scrape step of a saved automation, a column,
a comparison (<, <=, >, >=) and a number, at most 20 per automation, checked against the columns the step really has (known from its runs or
from the task it was saved from). Numbers come out of the cell text by code in any local format ("1 299,99 zł", "1.299,99", "$1,299.99",
"12.50 EUR"; a date like 3.5.2020 is not a number). After a scrape step of an automation run finishes, rows that satisfy a rule and did
NOT satisfy it in the previous run (matched by a unique column, otherwise as whole rows) are alerts: stored with the result, first lines
of the step's result, a `scrape.changes` event, and an entry under Updates in the Inbox even when nothing else changed. Panel: Automations ->
Watches (add / remove; columns offered from the last result) and the alert lines on the Updates card.
**Verified**: 436 tests (+34: the number formats, the comparisons, crossing the line once and again after it went back, alert-only news, new rows,
greater-than rules, plain tasks and other steps never alert, validation against real steps and columns, the cap, API incl. CSRF and 422, cascade
delete, a keyless table), ruff, build, live panel (add a watch with a decimal comma, the column list, the alert card). Mutation checks (12): every
match an alert, operators, columns, step kind, cap, dates as numbers, hidden from Updates, mark-all, keyless rows, not saved - all fail a test.
**Not verified**: real prices over days. Alerts stay in the panel (nothing leaves this machine; P6 adds an opt-in webhook).

## Loop run 17 - P3: retry one failed step
**Why**: when one of six steps failed (a site said no, a timeout), the only way forward was "Run again", which re-does everything that worked and
costs it again.
**Built**: `conductor.retry_subtask` and `POST /api/subtasks/{id}/retry`. The failed step is reset to a clean slate (pending, attempts 0, no
error or result), and so are the steps that were skipped because of it, and those skipped because of those. Everything else is untouched. The task goes
back to running, and the planner reviews again when they finish. Refused: not a failed step; the task is still running or was stopped; the step belongs
to an earlier round; the step already did something irreversible (`side_effects`), so retrying can't repeat a payment or a post. Panel: a "Retry this
step" button on exactly the steps that qualify. A retried step of an automation's run keeps the next scheduled run waiting (runs never overlap).
**Verified**: 441 tests (+5: end to end with the fake workers: the failed step, its dependents and theirs restart, an unrelated failure and its skipped step
stay, a clean slate right after the retry, retry twice, the whole refusal matrix incl. CSRF, side effects, automation runs), ruff, build, live panel (the
button on the retriable step only; click -> pending, event logged). Mutation checks (8): dependents, over-resetting, irreversible steps, rounds, task
status, step status, task not restarted, old error kept - all fail a test.

## Loop run 18 - P4: retention
**Why**: every browser step writes a screenshot, every browser-use run an agent history, every step a log line; nothing ever removed them.
**Built**: `fleet/retention.py` and `retention_days` among the editable limits (0 = keep everything, the default: deleting must be a choice).
When set, finished (done / failed / cancelled) tasks that ended longer ago than that lose their screenshots (a folder per step id), agent histories
(a file per step id) and plain `step` / `log` events, nothing else: the tasks, steps and answers, `scrape_results` (change monitoring and watches
depend on them), saved automations, and every audit event (human.request/response, approvals, trust rules, registrations, reveals, plan, status)
stay. Running or unfinished tasks are never touched (an old date on a task that never finished means nothing). Files are only deleted by the id of a
step in the database, inside the two data folders. The conductor's loop calls `run_if_due()`: it looks every ten minutes, cleans up at most every six
hours (remembered in the settings table across restarts) and leaves a `retention.pruned` event when it removed something. Panel: Settings -> Storage
(sizes, step log lines, "Clean up now"); the period itself is under Limits.
**Verified**: 450 tests (+9: what goes and what stays, recent / unfinished / ancient-but-unfinished, failed and stopped tasks, only step-named files, off by
default, twice is harmless, due / wait / throttle, the conductor calls it, API incl. CSRF and the off case, the ceiling), ruff, build, live panel (usage,
set 30 days, clean up: 6 files, 1.4 MB gone). Mutation checks (8): unfinished tasks, recent tasks, audit events, off, the six-hour wait, the throttle,
the endpoint's off case, the conductor hook - all fail a test.

## Loop run 19 - P5: the last two control switches from S4
**Built**: (1) a task can opt out of the operator's "always allow" rules (`tasks.use_trust_rules`, composer checkbox "Use my always-allow rules",
default on, copied by "run again" and by saved automations; an automation saved before the switch existed defaults to on). With it off every click that
needs approval asks even where a rule matches, and the rule's use counter doesn't move. (2) A list of sites Fleet never signs up on (`no_register_domains`,
Settings -> Your details): same address rules as saved logins, subdomains covered, look-alikes (`evil-shop.example.com`) not, checked before the operator is
asked, whatever the task allows. Migration 018.
**Verified**: 5 tests (the switch end to end against a real rule, stored / run again / automations / old automations, list semantics, API incl. CSRF, the
registration refusal and the card coming back after removal), ruff, build, live panel (add / invalid / remove, the composer checkbox). Mutation checks (7):
rules used despite the switch, run again, automation save, default for old automations, look-alike matching, address cleaning, the registration check.

## Loop run 20 - P6 notifications and P7 the first-real-runs checklist
**P6 built**: `fleet/notify.py`. Opt-in only: nothing is sent until the operator pastes a webhook address, ticks what they want and presses "Send a test".
The address is a secret (webhook addresses carry a token): stored in the vault, never returned by the API (only the host is shown). It must be https, on a public
host and port 443, checked again after DNS right before every send (DNS may change), redirects are never followed, no embedded credentials. One short line per
event: `human.request` (something needs you), `scrape.changes` (changes found or a watch line crossed), `task.status` done / failed; step and log chatter never
becomes a message and doesn't even start a background job. Keys are scrubbed from the text, which is capped at 400 characters; at most 20 an hour, then a single
`notify.paused` event; a failed delivery becomes a `notify.failed` event that never contains the address (only the error class) and never touches the work
(delivery runs in a background job). JSON (Discord / Slack style) or plain text (ntfy style). Panel: Settings -> Notifications.
**Verified (P6)**: 477 tests (+22) with a local receiver standing in for the webhook: address rules, encryption and never-returned, both formats, scrubbing and the
cap, redirects, the send-time re-check, which events become which line, chatter, the hourly limit, 500s and a dead receiver, an error text that contains the
address, the API incl. CSRF / keep / remove. 12 mutation checks. Live panel: renders, refuses an http address, saves nothing (no outbound call was made).
**P7 built**: `FIRST_REAL_RUNS.md`: nine cheap, ordered checks for what needs credits or a real AdsPower profile (the model's answer, scrape recipe learning,
item pages, save + rerun + watch, crawl, a fast-browser step with pause / take over, a saved login incl. two-step, a sign-up, notifications), each with what good
looks like, the expected cost and what to write down.

## Loop run 21 - P8: the documentation matches the code, and a test keeps it that way
**Built**: `tests/test_docs.py` (no database): every `FLEET_*` variable named in README / RULES / .env.example / FIRST_REAL_RUNS / the roadmap must be a real setting
(two documented exceptions: the master key the vault reads itself, the test database URL), every `fleet/...py` / `tests/...py` file those documents point to
must exist, and every changeable limit must be listed in .env.example. It immediately found a real gap (`FLEET_RETENTION_DAYS` was missing from .env.example). Also:
the README worker table and diagram list scrape / crawl / browser_fast, the README points to the checklist, roadmap and ideas; the roadmap's "Left" notes no
longer list what P2-P6 built; FUTURE_IDEAS lost the two items that are now built.
**Verified**: 481 tests (+4); mutation checks: an invented setting, an invented file, a limit missing from .env.example - all fail the test.


## Loop run 22 - the first runs with a real OpenRouter key (about $2.3 of a $6 allowance)
The operator topped up OpenRouter and allowed up to $6 of real tests: a second Fleet on a throwaway database with a headless Edge standing in for AdsPower
(plus the real server for the first checks). Everything below was run with real models; all earlier work had been tested with stand-ins only. What the real runs found,
and what was done about each:
1. **Full browser agent never worked** (4/4 failed). Two stacked causes. (a) browser-use sends `temperature` / `top_p` / `seed` as JSON `null`; with
   `provider.require_parameters` OpenRouter reads each as a required parameter and no Anthropic route has them: 404 "No endpoints found". (b) Once that was gone:
   the agent's action schema (~17 kB) as `response_format` gives 400 "compiled grammar is too large" on every Anthropic route, strict or not. Fix: `FleetChat`
   (`executors/browser.py`) asks for the JSON in the system prompt and validates the reply itself, tolerant of a code fence or a sentence around it; no
   `response_format`, no `require_parameters`.
2. **The full agent clicked "Submit order" without asking** (it never called its own `request_approval` tool). Approval for the full agent was prompt-only. Now
   `fleet/click_approval.py` is the one implementation both browsers use, and the full agent's `click` action is wrapped (`FleetTools`), also for clicks by
   position, with a one-action pre-approval when the operator just answered the model's own `request_approval`. Re-run for real: the Inbox card appeared
   before the click on both engines, the answer "reject" left the form unsent.
3. **Fast engine gave up on a page that was still loading** ("Document is navigating" after half a second), handing a working task to the full agent. `observe()`
   now waits up to 12 s with a growing delay.
4. **Scrape tasks got a pointless second planner round** that re-read the pages and called rows "not captured": the result text now says the table is complete and
   saved, and the reviewer prompt forbids re-reading. A saved run made only of scrape / crawl steps is summed up from their own reports with no model call, so an
   automation run costs exactly $0.0000 (measured).
5. **A login form next to a sign-up form** (the shape of news.ycombinator.com/login) was classified as a sign-up because the page held two password fields; only the
   fields of the same form count now. The sign-up planner deliberately still refuses such a page (it must agree with the login detector; test added).
**Measured costs** (the real numbers for FIRST_REAL_RUNS.md): fast browser step on Wikipedia 3 steps $0.017 (task $0.055 with planner and review); the same
with the full agent 5 steps $0.17 (task $0.22); take-over on the real agent: asked at step 1, paused at step 2, nothing moved for 8 s, continued on
"give control back" ($0.23); scrape of 2 pages $0.083 then the saved rerun $0; crawl of docs.python.org/3/tutorial 17 pages $0.06 (crawl itself $0);
Polish research with sources $0.115; approval test full agent $0.26 / fast $0.05; captcha hand-over on Google's reCAPTCHA demo: fast $0.05 / full $0.09 (both
raised the card, neither tried to solve it). Fleet's own politeness showed too: the planner's verification fetch of a site whose robots.txt disallows it was refused.
**Known, left as is**: Facebook's login form sits inside an `aria-hidden` wrapper and Fleet never types into those (honeypot protection), so the operator logs in by
hand there; sites that block a headless browser (Reddit, Amazon, PayPal, Allegro showed no inputs at all) need the real AdsPower profile, which this stand-in isn't.
**Not done by design**: no real account was signed into or created. The operator's RybiaPaka test login was not typed by Claude: signing in to a hosted site with their
password is theirs to do (save it under Settings -> Site logins and approve the Inbox card).
**Verified**: 7 new tests in `test_fastbrowser_unit.py` (the schema in the system prompt, the reply however the model wraps it, a reply that doesn't fit, the click
gate for labels / ordinary clicks / clicks by position / pre-approval), 1 slow-page engine test, 2 login/sign-up-page tests, 4 table-only-run tests; mutation checks
on each fix (11) fail the matching test.


## Loop run 23 - the definition of ready, the gate runner, the loop, and F6 embedded Postgres (2026-10-18)
The operator's brief: say what makes Fleet *ready* (an agentic browser that does any task with fewer limits than the browser's built-in AI,
cheaper because the brain plans once and cheap hands execute, recorded so a task done once is repeatable, driven by voice), then build a loop
that works until it gets there, with tests it can improve itself against, using an OpenRouter key and a Claude API key from `.env`.
**Built**
- `READINESS.md`: five promises (any task, brain/hands, record/replay, voice, cheaper) plus foundation and rules; 35 gates (S, F, A, B, R, V,
  C, H) with a pass condition, a level (L0 offline, L1 keys, L2 live sites, L3 the operator) and a priority order; the exit rule.
- `python -m readiness` (`readiness/run.py`, registry in `readiness/registry.py`): one pytest session over `tests/` and one over
  `readiness/gates/`, read back from JUnit; command gates (imports, ruff, panel build, server boot, embedded boot); human gates from
  `readiness/signoff.json`; live gates only with a key and within `FLEET_READINESS_BUDGET_USD` (every call in `readiness/spend.jsonl`);
  writes `readiness/scorecard.json` and `READINESS_STATUS.md`; exit 0 ready / 2 only sign-off left / 1 failing.
- Gate tests in `readiness/gates/`: each failing one documents the expected shape in its docstring (the loop's brief); fixtures in
  `readiness/fixtures/` (form with select + checkbox, contenteditable editor, same-origin iframe, file upload, new tab, a redesigned shop
  for self-healing replay); `readiness/benchmarks/voice_utterances.json` (51 Polish/English utterances → commands).
- The loop: `LOOP.md`, `scripts/loop.py` (readiness → first failing gate → `claude -p` with the skill → readiness; history, logs, cost and
  time caps, stalled-gate handling, `--dry-run`), `.claude/skills/fleet-loop/SKILL.md` (the iteration procedure: one gate, one verified
  change, keep or revert, commit, log). `scripts/bootstrap.ps1`, `scripts/pgtest.py` (throwaway Postgres for hand-run tests).
- `.env` / `.env.example`: `OPENROUTER_API_KEY`, `ANTHROPIC_API_KEY` (never overriding the environment), `FLEET_DATABASE_URL=embedded`,
  `FLEET_READINESS_BUDGET_USD`. Settings gained `readiness_budget_usd`.
- **F6**: `FLEET_DATABASE_URL=embedded` starts Fleet's own Postgres under `data/pg` through the `pgserver` package (`db.dsn()`, used by the
  pool, `ensure_database` and the event listener); `pip install -e .[embedded]` / `uv sync --extra embedded`; readable error without the
  package. `db.close()` now terminates a pool whose connections a stray task still holds (a failing pipeline test used to poison the next test
  with "Event loop is closed").
- The operator added (mid-pass) that Fleet must build a **base of self-improvement**: concrete actions and their results. Added as the
  sixth promise and gates K1-K5 (`experience` table written after every browser step with domain, goal, actions, outcome, reason,
  cost, seconds; `best_path` / `site_notes` queries; the hands follow a known path and the planner reads site notes as data;
  an API + Knowledge page to read and prune; a live check that a paraphrased second visit costs <= 30%). Tests in
  `readiness/gates/test_k*.py` describe the shape; all red until the loop builds it.
**Found while building**: the session folder's path is too long for the Store Python and git on this PC (venv failed); the work was done from
a short folder. `/api/health` takes up to 5 s when AdsPower is absent (it probes the Local API with a 5 s timeout): the boot gates probe with
a 15 s timeout; worth caching later. Fixture pages without `<meta charset>` show "»" as "Â»" in a headless browser, so gate scripts match
labels by prefix.
**Verified**: first full quick pass 10 pass / 15 fail / 6 unavailable / 4 waiting; after fixing the harness: S1, S2, F1-F6, B1, R1, R2,
R5, A2, C1, C3 pass; A1, B2, R3, C2, V1-V4, A3, A4, A6, C4 fail with the gap named (missing module, missing route, "no click 'Reveal
secret' among the offered actions"...). `tests/test_db_embedded.py` (3 tests) covers F6's resolution and its error; 287 named tests + the
whole suite green on the embedded Postgres; ruff clean; panel builds. Loop dry-run picks A1 first.
**Final pass** (`python -m readiness`, 19 min, 502 tests): 16 pass / 20 fail / 4 waiting for the operator. Live gates for $0.0066 in total:
Jev decision $0.000078 at 0.3-1.7 s (B3 pass), a typed value on Haiku $0.0004 (B4 pass); real Jev on the fixture shop fills the search and
clicks Search, then reports BLOCKED on the results page (R4, K5: the goal wording or the result links need work); live benchmark 2/3:
Wikipedia search → Gdańsk article $0.0033 and quotes.toscrape next page $0.0006 done, books.toscrape Mystery category blocked at $0.0002.
**Not done by design**: no feature gate was implemented beyond F6 (one gate per iteration); nothing pushed.


## Loop run 24 - A1: local Chrome, no AdsPower (2026-10-18)
The first gate the loop picked after the readiness pass: every browser step needed an AdsPower profile (paid plan, Local API, not
installed on this machine), so nothing browser-related could be exercised for real here.
**Built**: `fleet/browsers.py`, the one way to open a profile's browser: `open(profile)` returns a CDP address for an AdsPower profile
(through the Local API as before) or a *local* profile, a Chrome / Edge Fleet starts itself with its own user data directory under
`data/profiles/<id>` (headless by default; `visible=True` for the operator's "Open"), one process per profile per worker, closed with
the step and at exit; readable errors (no browser found → set `FLEET_BROWSER_EXE`). Migration 019 (`profiles.kind`, `headless`,
`data_dir`; `adspower_id` = `local:<random>` for local rows). `profiles.add_local`, `fleet profiles add-local <name> [--visible]`,
`POST /api/profiles/local`, "Add a local profile" on the Profiles page, "Open" shows a headless profile's window. Both browser workers
go through `browsers.open` / `browsers.close`; Inbox texts say "the browser window for profile X" (and that a headless profile must
be opened from the Profiles page first) instead of "the AdsPower window". RULES §1 and §5, README setup, `.env.example`.
**Verified**: gate A1 (3 tests) passes; `tests/test_local_browser.py` (6 tests: a real headless Chrome opened, attached over CDP,
reused on a second open, closed; AdsPower rows still go through the Local API; missing browser error; operator wording; add_local;
executors and API never call AdsPower directly); profiles / logins / trust / registration / API sweep / control suites green
(143 tests); mutation (local profiles dispatched as AdsPower) fails 4 tests; ruff clean; panel builds. Full pass below.
**Also fixed on the way**: gate F6 failed on every fresh data directory because the readiness runner read the booted server's
output through a pipe it never drained; a first-time embedded Postgres prints initdb's whole report through that pipe, the
server blocked on the write and never listened. The runner now logs the server to a file (unbuffered) and keeps it under
`FLEET_READINESS_KEEP_LOGS=1`. `fleet serve` runs its database check and uvicorn in one event loop. A killed `fleet serve` leaves
the embedded Postgres running (pgserver stops it only on a clean exit); the next start attaches to it, so nothing is lost.
**Left**: the live gates (A7, R4, K5) still drive the test browser directly; the panel shows a local profile's `local:` id where the
AdsPower id used to be (cosmetic); no per-profile proxy for local profiles by design (RULES §5).


## Loop run 25 - B2: the brain writes a macro (2026-10-18)
The second promise in READINESS.md is that the brain decides *how* once and cheap hands execute. The recorded paths and scrape recipes
already do that for things Fleet has done; a macro is the same idea for things the brain can *write down* before anything runs.
**Built**: `fleet/macro.py`: a pydantic `Macro` of steps (navigate, click, fill, select, wait_for, extract, back, scroll, assert_text),
`validate()` that names the broken step (unknown kind, missing field, non-http URL) and refuses outright any fill into a password / card /
one-time-code field (English and Polish labels; the page reader never offers those fields anyway), and `run(cdp, macro, hooks=...)`: the
steps executed through the fast browser's page reader and hit-tested inputs with no model call, under the engine's gates (an element must
be on the page under that name, exact or prefix; a commitment click goes through `policy.needs_approval` → `hooks.approve`; private
addresses and domains outside the allowlist are refused; `hooks.should_stop` / `on_page` every step). Stops honestly at the first step it
can't do: `blocked` (missing element, with the labels that were offered), `stuck` (wait_for / assert_text not met), `rejected`, `stopped`,
`timeout`, `error`. Returns a `MacroOutcome` (the engine's Outcome plus `extracted`) whose trace is the same replayable fill/click path
the engine records. README section "Macros".
**Verified**: gate B2 (3 tests) passes; `tests/test_macro.py` (9 tests: validation messages, seven secret labels refused, the shop search end
to end with $0 and the expected trace and steps, approval refused then granted on a real page, missing element / unmet wait / click before
navigate, private address refused, select-back-scroll on the listing pages); mutation checks: approval gate removed → 2 tests fail, secret
labels allowed → 8 fail; ruff clean. Full pass: 521 tests green, 17 gates pass / 14 fail / 5 live unavailable offline / 4 for the operator.
**Also fixed on the way**: the readiness runner killed only the venv's python launcher, not the server behind it (a stray `fleet serve` and
an open log file broke the next pass); it now kills the process tree. The embedded-boot gate starts from an empty data directory every
time and stops and removes its own Postgres afterwards (a dirty directory from a hard kill made Fleet report, correctly, that the embedded
server couldn't start).
**Not done by design**: no worker kind or planner prompt for macros yet (that is the brain's side: a `macro` step the planner emits for a
site it knows, fed from the experience base, K-gates); no panel view. Both belong to later gates, not this one.


## Loop run 26 - K1: every step is written down (2026-10-18)
The first gate of the experience base, the pillar the operator asked for by name: Fleet must build a base of self-improvement from
concrete actions and their results, not only keep paths it can replay.
**Built**: migration 020 `experience` (domain, goal, kind, status, reason, actions jsonb, cost_usd, seconds, task_id, subtask_id,
created_at; index on domain + time). `fleet/experience.py`: `domain_of(url)` (host, lower-case, no port) and `record(ctx, status,
reason, actions, seconds)`, which writes one row per run from the step's own words and the engine's own words (the instructions as the
goal, the replayable path as actions, the one-line reason redacted through the vault and capped at 500 characters) and never raises (a
problem writing experience is logged, the step's result stands). Both browser workers call it after every run, whatever the outcome:
the fast worker once per attempt (a hand-over to the full agent is a second record from the full agent), the full agent with
`done` / `failed` and a reason that is a state, never the model's prose. RULES.md §7 (what is kept, what is never kept) and a README
section; the RULES "Enforced by" line names `fleet/experience.py` and `tests/test_experience.py`.
**Verified**: gate K1 (2 tests through the real worker, done and rejected runs) passes; `tests/test_experience.py` (domain parsing,
a full row, redaction and capping, a failing database does not fail the step, both workers call record); worker suites
(fastbrowser pipeline, site logins, trust, docs) green; mutation (the fast worker's record call removed) fails the gate; ruff clean.
Full pass: 531 tests green, 18 gates pass / 13 fail / 5 live unavailable offline / 4 for the operator. Gate stubs for K1/K3/R3 learned the `kind` attribute the workers log since A1 (a harness fix, not a product one).
**Not done by design**: nothing reads the base yet (K2 queries, K3 use by brain and hands, K4 the panel); the macro runner records
nothing because it is not a worker yet.

## Loop run 27 - R3: self-healing replay (2026-10-18)
A saved path broke the moment a site renamed one button: the engine already asked Jev where a recorded step didn't fit, but it never
said that a step had been replaced and nobody wrote the new path back, so every later replay paid for the same change again.
**Built**: `fleet/fastbrowser/engine.py`: `replaces(step, action)` (Jev's action stands in for the recorded step when it is the same
kind of action under another label; a different kind is an extra step, not a replacement); a healed step moves the path on, counts in
the new `Outcome.healed`, is marked `healed` in the step record (the activity says "Jev, in place of a recorded step"), and a healed
fill types the recorded value, so one renamed field costs one decision and no text call. `fleet/automations.py`:
`update_trace(automation_id, key, trace)` replaces only that browser step's trace under a row lock and says whether anything changed.
`fleet/executors/browser_fast.py`: a finished replay of an automation step with `healed > 0` writes its trace back through
`update_trace` and logs "Path updated: N of M recorded steps had changed on the page"; never after a hand-over to the full agent
(its path is its own) or after a rejected/stopped run (the code returns before), and a failed write is logged, the step's result stands.
README "Automations" says so.
**Verified**: gate R3 (2 tests: one renamed button on shop_v2.html costs one decision plus the final DONE and the trace carries "Find";
through the worker the automation's stored step changes) passes; `tests/test_self_healing.py` (4 tests: `replaces` on same/other
label, other kind, back, None; the real headless browser on shop_v2.html with `healed == 1`, hint `click "Search"` and the text model
never asked; shop.html heals nothing; `update_trace` changes only that step and refuses non-browser steps, unknown keys and an
unchanged path) and `tests/test_fastbrowser_pipeline.py::test_a_healed_replay_updates_the_saved_path_but_a_hand_over_or_a_refusal_never_does`
(healed replay → stored path and one "Path updated" event; stuck → full agent, no write; rejected → step failed, no write);
mutation (the write-back call removed) fails the gate; ruff clean. Full pass: 536 tests green (15 min against the embedded Postgres).
**Measured**: the drifted replay on the fixture costs exactly 2 Jev calls (one for the renamed step, one DONE), $0 with the scripted model.
**Not done by design**: a step Jev adds on the way (a banner to close) is not a healed step and does not rewrite the path, nor does a
recorded step the run skipped; only a label change is paid for once. The macro runner and the full agent don't heal paths (they have no
guide). Whether a healed path should also be a note in the experience base belongs to K3.

## Loop run 28 - C2: Claude API as a brain provider (2026-10-19)
The operator pays Anthropic for Claude already, but every Fleet call went through OpenRouter's margin, and the vault didn't even know
the word ANTHROPIC_API_KEY. The brain should be able to run on the key they already have, without the cheap roles following it to an
API where they don't exist.
**Built**: `fleet/providers.py` — one place that decides which API a model id goes to. A model written `anthropic-api/<model>` goes to
`POST https://api.anthropic.com/v1/messages` with `x-api-key` from the vault (`vault.get` already falls back to the environment),
`anthropic-version: 2023-06-01`, the system turn lifted into the `system` field, OpenRouter-only extras (`response_format`, `provider`,
`plugins`) dropped rather than sent and refused, and `thinking` deliberately never sent (unset is the one spelling every Claude model
accepts). Retries and one readable error per status, the same shape as `openrouter.py`, with the detail redacted through the vault.
Cost comes from a price table (USD per million tokens, Oct 2026: opus-5 5/25, sonnet-5 3/15, haiku-4-5 1/5, fable-5 10/50) and is worked
out the way Anthropic bills: fresh input at full price, prompt-cache writes at 1.25x, cache reads at a tenth, while the token counters
stay the whole prompt the way OpenRouter reports it. `fleet/llm.py` now calls `providers.chat` / `providers.text_of` instead of
OpenRouter directly (on Anthropic the JSON schema is asked for in the prompt, validated by the same pydantic round, and the repair round's
assistant turn is never the last message, so it is history and not a prefill — a prefill is refused on these models); `Usage.add` takes its
fallback price from `providers.price_per_token`, which is what makes a direct browser step cost anything at all, since browser-use reports
tokens and no money. `fleet/models.py`: a `providers` tuple on the planner / smart / fast / browser roles, read through the new
`allows_provider(role, provider)` and enforced in `update()`, which skips the OpenRouter catalog check for a direct id and refuses a model
Fleet has no price for (so a direct call can never be billed at a price it doesn't know). `fleet/executors/browser.py` builds browser-use's
own `ChatAnthropic` for a direct model (the action schema goes through Anthropic's tool use, which takes it as it is, so the
JSON-in-the-prompt workaround OpenRouter needs isn't) and checks the Claude key, not the OpenRouter one, before opening a browser.
`vault.KNOWN` learns ANTHROPIC_API_KEY as optional; `/api/models` appends the direct ids so the panel's picker offers them to exactly the
roles allowed to use them (`providers` on each role, filtered in `web/src/pages/Settings.tsx`); README, RULES.md §9 ("page content is sent
only to the model API the chosen model names", enforced by `fleet/providers.py`) and `.env.example` say so.
**Verified**: gate C2 (3 tests) passes; `tests/test_providers.py` (29 tests: prefix routing and bare names, the request shape and headers,
the system field, dropped extras, text read from text blocks only, cost from the table, cached tokens counted in full and billed 1.25x/0.1x,
`add_tokens` priced from the table, which roles may and may not, 401/403/404/400 fail fast with advice and no retry, 429/500/529 retried,
truncation and refusal reported, a missing key sends nothing, an unreadable key is not retried, an error never leaks the key, an OpenRouter
id still goes to OpenRouter); mutation checks: `is_direct` forced False → the gate's routing test fails (and 23 of the new tests),
`allows_provider` forced True → the gate's cheap-roles test fails (and 2 of the new tests); both restored. Full pass: 565 tests green in
17m51s against the test Postgres (536 before, +29 new); ruff clean; `npm run build` clean.
**Measured**: a planner call on `anthropic-api/claude-opus-5` with 120 input and 8 output tokens is accounted at $0.0008; a million cached
input tokens cost $6.75 against $5.00 fresh (write) and $0.50 (read). $0 spent this iteration — every test runs against a fake Messages API.
**Not done by design**: no live call to api.anthropic.com was made, so the direct route is verified against a fake API and the price table,
not against Anthropic itself (the C2 row's "L1 live" half, and the browser role's direct client in particular, is still unproven on a real
page). Prompt caching is priced but never asked for (no `cache_control` is sent yet, so the cache fields are always zero in practice);
the price table is hand-maintained, so a price change needs an edit here, which is the deliberate trade for refusing a model Fleet can't
account for. Jev stays on OpenRouter's decisions API, research on Perplexity, video on Gemini — they have no Anthropic equivalent.


## Loop run 29 - C5: a simple task costs cents (2026-10-19, Opus, cut off at 120 turns; committed by the operator's session)
The loop's first iteration on Opus took the new gate C5 (a one-step task through the whole of Fleet for <= $0.03, its saved replay
<= $0.005) and made it pass, but hit the iteration's turn cap before it could commit and write this entry, so the operator's
session committed the verified tree as is.
**Built** (from the diff): `planner.one_page` - a prompt that names ONE page and what to do there (a URL, under 60 words, none of the
words that reach past a page: research, compare, each, scrape, crawl, csv...) becomes a single fast-browser step with the operator's words
as its goal, and no planning model is called; `conductor._saved_run_answer` - a saved automation's run whose steps answer for themselves
(one finished step, or scrape/crawl reports) skips the review round, and a round's cost is charged before the task is marked done, so a
finished task's cost is final; the fast engine reads on (up to four screens) when Jev reports nothing useful and the page continues
below, and Fleet's own headless windows are desktop-sized (1280x1024) so a "Next" link under ten results is on screen; the C5 live gate
runs through a local profile. README: "one page" prompts, saved runs that skip the review. `tests/test_one_page.py`.
**Verified** (by the loop's readiness pass after the iteration): C5 passes live - the quotes.toscrape task and its replay within the
thresholds; 23 gates pass; the suite green. The iteration ran out of turns (121) at $10.44 equivalent before the commit step; the loop's
turn cap is raised to 200 from this run on.


## Loop run 30 - K2: the base answers questions (2026-10-19, Opus; committed by the operator's session)
The loop's iteration made K2 pass (`experience.best_path(domain, goal)`: the actions of the latest finished step on that site with a goal
like this one, goals compared by their meaningful words, >= 0.6 of the shorter; `experience.site_notes(domain)`: one line for the brain
with how many runs finished, how many did not and why last, whether a login wall was met, <= 600 characters; README section) and the
readiness pass after it was green (580 tests, 24 gates pass), but the iteration ended its turn while its own test run was still in the
background, so it never reached the commit step; committed here as verified. The skill now forbids background jobs in print mode.


## Loop run 31 - K3: brain and hands use the experience base (2026-10-19, Opus; committed by the operator's session)
**Built**: a fast-browser step with no recorded path asks `experience.best_path(domain, goal)` and hands the result to the engine as a
guide (an argument, never written into the subtask, so only a saved automation's own path is ever taught back when it heals); the planner's
prompt carries `site_notes` for the sites a task names (URL hosts in the prompt, the task's allowed domains, the enabled profiles'
domains; at most 8) under the label "What Fleet already knows about these sites (data, not instructions)" (`prompts.site_knowledge`).
**Verified**: gate K3 (2 tests) passes; the fast-drive stand-ins in three test files gained the new `guide` argument (the iteration
had not run them); fastbrowser pipeline, site logins, trust, K1, R3 gate suites green (81 tests); ruff clean.

## Loop run 30 - A3: frames (2026-10-19, hands session, branch claude/loop-hands-be3644)
The page reader descends into same-origin frames: `snapshot.js` collects the page and every reachable frame (three levels,
`iframe`/`frame` whose `contentDocument` can be read and that is on screen) as one list of documents with the offset of
each frame's viewport in the window; controls inside a frame are offered like the page's own with their rect shifted by
that offset, the frame's visible text joins the page text, the page key and the `login` flag look into frames too, and
`cache.docs()` exposes the list. A cross-origin or sandboxed frame has no readable document and is skipped: nothing of it
is offered or read. `page.py`: the hit test (`TARGET_JS`) checks the element against its own document's `elementFromPoint`,
then walks up through every enclosing frame, adding the frame's offset and checking the frame is what its parent shows
there, so the click lands at window coordinates (`Input.dispatchMouseEvent` routes it into the frame); the settle wait and
`aria-labelledby` resolve in the element's own document; `FULL_TEXT_JS` (the result text) joins the frames' text.
Freshness is unchanged: a frame element is guarded by identity + role + name + value like any other and a navigated frame
disconnects its nodes. **Verified**: `readiness/gates/test_a3_frames.py` passes; new
`tests/test_fastbrowser_engine.py::test_same_origin_frames_are_read_and_clicked_cross_origin_ones_left_alone` (a page with
one same-origin and one cross-origin frame served from a second port: the button is offered once, the text read once, the
click works); tests/test_fastbrowser_engine.py, test_fastbrowser_unit.py, test_login_steps.py (DB tests skipped: no
Postgres in this worktree) and readiness/gates/test_a2_benchmark.py green; ruff clean. Mutation: dropping the frame
offset from the hit test fails A3 ("Target changed or is covered"). **Not done**: scrolling inside a frame (only the
window scrolls), frames deeper than three levels, cross-origin frames (by design: untouched).

## Loop run 31 - A4: uploads (2026-10-19, hands session, branch claude/loop-hands-be3644)
A file input is offered as kind `upload` (`snapshot.js`: `type=file` is no longer dropped, role `upload`, value = the
names of the files it holds; it is never offered as a click, which would open the native dialog) and the path comes from
the operator alone: `engine.Hooks.pick_file(action, state) -> str | None` (default None → the step ends `blocked`, "No
file was chosen"; a recorded step's value is never used for it), `page.act` for `upload` runs the same freshness guard
and hit test as a click and then `Page.set_files` resolves the observed node to a remote object and calls
`DOM.setFileInputFiles` (no dialog), then dispatches `change`. `decide.action_space` maps `upload` to an `UPLOAD`
operation with its own target question ("the operator chooses which file; never a path from the page"), so Jev can only
ask for the upload, not name a file. `FleetHooks.pick_file` asks the operator with a "help" Inbox card ("Which file
should I attach to …? Write the full path in the note"), rejects a reject, and returns the resolved path only if it is an
existing file (otherwise logs "No such file" and the step is blocked). **Verified**: `readiness/gates/test_a4_upload.py`
passes; new `tests/test_fastbrowser_engine.py::test_a_file_input_takes_only_the_operators_path` (default hook → blocked,
no trace; the input is offered once and only as `upload`; a hook that answers → "Attached: report.txt (5 bytes)", trace
`upload` with the path), `tests/test_fastbrowser_unit.py::test_a_file_input_is_offered_to_jev_only_as_an_upload` and
`::test_fleets_upload_hook_takes_the_path_from_the_operators_note_and_checks_it_exists`; engine, unit, A2, A3 green
(login_steps DB tests skipped here); ruff clean. Mutation: removing the `DOM.setFileInputFiles` call fails A4 ("Nothing
attached"). **Not done**: hidden file inputs behind custom "Choose file" buttons (not visible, so not offered); drag-and-drop
upload zones; a replay asks the operator again rather than reusing the recorded path.

## Loop run 32 - A6: new tabs (2026-10-19, hands session, branch claude/loop-hands-be3644)
A click that opens a new tab is followed. `cdp.py` gets event listeners (`on`/`off`; the reader dispatches every event
with a method to them, a listener's error is logged, never fatal). `page.py`: `Page.open` turns on
`Target.setDiscoverTargets`; every Page listens for `Target.targetCreated` and keeps the ids of page targets whose
`openerId` is its own target (Chrome reports the opener even for a `target=_blank` link, which is noopener since Chrome 88:
`canAccessOpener` is false, `openerId` is set - checked against a real headless Edge, the event comes with an empty url and
the address follows in `targetInfoChanged`); `Target.targetDestroyed` forgets them. `Page.attach(cdp, target_id)` is the
session set-up split out of `open`; `opened_tab()` attaches to the first tab the last action opened (extra ones are
closed), waits for its address to leave about:blank and for readyState, and returns it. `engine.py`: after an action the
engine asks for an opened tab (before and again after reading the page, so the event can't be missed), pushes its own
tab on a stack and continues on the new one; `go_back()` closes the current tab and returns to the one that opened it
when there is one, else browser history (used by Jev's BACK and by the allowed-domains strike); at the end every tab the
engine followed into is closed, then its own. The new tab's address goes through the same loop-top checks as any page
(netguard, allowed domains), nothing was relaxed. **Verified**: `readiness/gates/test_a6_new_tab.py` passes; new
`tests/test_fastbrowser_engine.py::test_a_click_that_opens_a_new_tab_is_followed_and_every_tab_is_closed_at_the_end`
(the report is read in the new tab, `Target.getTargets` shows neither tab afterwards),
`::test_back_from_a_new_tab_closes_it_and_returns_to_the_tab_that_opened_it` (trace click, back; pages reports, report,
reports) and `::test_a_new_tab_outside_the_allowed_domains_is_closed_and_the_engine_returns` (a `localhost` tab under an
allowlist of 127.0.0.1 is closed and the engine goes on in its own tab); A3, A4, engine, unit, A2 green (login_steps DB
tests skipped here); ruff clean. Mutation: not remembering opened targets fails A6 (the opener page is read, no
"Opened in new tab"). Also in this run, commit "tests: ...": the A3 commit's insertion had swallowed the `def` line of
`test_private_addresses_are_refused` (its body ran as the tail of the frames test) and so the A4 engine test named in
run 31 had not landed; both restored. **Not done**: a tab opened later by a script (not by the step's action) is only
noticed after the next action; `window.open` from a cross-origin frame has that frame's opener, not ours, and is not
followed; downloads triggered by a click are not handled.


## Loop run 30 - C4: cost report (2026-10-19, side session `loop-panel-api`)
The bill is visible. `GET /api/costs/summary?days=30` (1..365) returns spend per role for the period and first run vs replays per
automation; Settings → Costs shows it.
**Built**: `fleet/costs_report.py` - `summary(days)`: tasks created in the period; `by_role` is the sum of `subtasks.cost_usd` grouped by
the step's kind plus a "planner" row for whatever `tasks.cost_usd` holds beyond its steps (planning, review, summaries; clamped at 0),
biggest spender first; `automations` lists every automation whose source task or replays fall in the period with `first_run_usd` (the
source task's bill), `replays` and `replay_total_usd` (tasks with that `automation_id`, in the period) and
`saved_usd = replays × first_run_usd − replay_total_usd` (negative when replays got dearer than the first run - a drifted path). Only
aggregates leave the API. `api.py`: one route under the same guards (`Query(30, ge=1, le=365)`). Panel: `api.costSummary`, a Costs
section in `web/src/pages/Settings.tsx` (7 / 30 / 90 days, a roles table, an automations table with the saving in red when negative).
README "Costs".
**Verified**: gate C4 passes; `tests/test_costs_report.py` (7 tests: roles from steps and the planner remainder, the remainder never
negative, the period counted from task creation, first run vs replays with an out-of-period run left out, a negative saving, an
automation without a source task and one without runs in the period, the route's shape and bounds); `tests/test_api_sweep.py` green
(the new route is guarded like the rest); mutation check: `saved_usd` replaced by `replay_total_usd` → the gate fails, restored. Ruff
clean, `npm run build` clean.
**Measured**: nothing spent - the report reads what the tables already hold; the three queries run in one round trip each.
**Not done by design**: the period is by task creation, not by when the money was spent (a task running over midnight counts on the day
it started); spend is per role, not per model (the tables don't record which model a step used). No chart - a table says the same.


## Loop run 31 - K4: the operator can read and prune the experience base (2026-10-19, side session `loop-panel-api`)
What Fleet learned is visible per site and can be forgotten one entry at a time.
**Built**: `fleet/experience_api.py` (a new module, so `fleet/experience.py` and its `record()` stay untouched for the session working on
the lookup queries): `entries(domain, limit)` - newest first, at most 200, the domain matched as it was written down (lower-case,
trimmed), every column (the goal in the operator's words, the replayable path, the outcome and its redacted reason, cost, time, the
task) - nothing in the table was ever a secret or page text; `sites()` - every domain with its entry count, how many ended well
(done / goal_achieved), the cost and the last visit; `forget(id)` - one delete, False when nothing was there. `api.py`: `GET
/api/experience?domain=&limit=` (limit bounded 1..200), `GET /api/experience/sites`, `DELETE /api/experience/{id}` (404 when gone),
guarded like every other route. Panel: `web/src/pages/Knowledge.tsx` (a site picker with counts, one card per entry with its status,
kind, goal, reason, the path on request, a link to the task and a Forget button), a "Knowledge" entry in the App navigation,
`api.experienceSites / experience / forgetExperience`. README: the Knowledge page under "The experience base".
**Verified**: gate K4 passes (both tests); `tests/test_experience_api.py` (6 tests: per site, newest first, every column; the domain
match; the 200 cap and the limit bounds; sites with counts and successes; forget once and not twice; the three routes end to end with
the CSRF guard on DELETE); `tests/test_api_sweep.py` green; mutation check: the listing turned oldest-first → the unit test fails,
restored. Ruff clean, `npm run build` clean.
**Measured**: nothing spent; a listing is one indexed query (`experience_domain_idx` covers domain + created_at desc).
**Not done by design**: no "forget the whole site" button (one DELETE per entry keeps the operator looking at what goes); no editing of
an entry (what Fleet learned is what happened, not something to rewrite); no search across goals yet.

## Loop run 30 - V1: voice grammar (2026-10-19, voice session, branch claude/loop-voice-1362e9)
The first of the four voice gates: spoken words become commands with no model in the loop.
**Built**: `src/fleet/voice.py` - `parse(text) -> Command | None` (a `Command` has `kind` and `args`; `to_dict()`), a regular-expression
grammar over a whitespace-folded utterance (case kept in captures, so "Lizbony" stays "Lizbony"): one verb, then its object, Polish and
English side by side; kinds open (a spoken address gets https:// and a lowercased host; "open settings" is a click), search, click (a
label, or "the third result" / "trzeci wynik" as nth + what), type, scroll, back, forward, tab (next / prev / n), read, stop, approve,
reject, key (enter / escape...), task (the rest of the sentence), help. `describe(command)` says a command back in words and never repeats
what was typed. Secrets: a `type`, `search` or `task` that carries a password / PIN / CVV word or a 13-19 digit run becomes
`refused` with a reason that never holds the words. README "Voice".
**Verified**: gate V1 (2 tests: 51/51 on the benchmark, the three secret utterances refused); `tests/test_voice.py` (44 tests: 28
utterances, 5 non-commands, 9 secrets never repeated in repr/describe/args, a password mentioned in a label or a task is not a secret,
describe); mutation check: `_secret` forced False -> the gate's secret test fails; restored. Ruff clean.
**Measured**: 51 of 51 benchmark utterances right (the gate asks for 95%); a parse takes microseconds; $0 spent.
**Not done by design**: the grammar is a list, not a model: an utterance outside it (a question, a full sentence) is None, and the panel
asks again. Only `localhost`, dotted hosts and IPs count as addresses. V4 (a live session), V2 (the API) and V3 (the panel) follow.

## Loop run 31 - V4: voice latency (2026-10-19, voice session, branch claude/loop-voice-1362e9)
A parsed navigation command acts on a live page within a second, with the engine's gates in code.
**Built**: `fleet.voice.Session` in `src/fleet/voice.py` - `await Session.open(cdp, url, allow_private=False)` (netguard refuses a
private start page unless allowed, as the engine does), `await session.execute(command) -> {"ok", "said", "took"}`, `page_text()`,
`close()`, built on `fastbrowser.page.Page` (observe without a screenshot, act, back): open (netguard-checked), search (the page's own
search box + Enter, else DuckDuckGo), click (the engine's `_norm` label matching: exact, prefix, containment, all words; "the third
result" is the nth link after `filter_actions`), type (the focused field, never a password / card / one-time-code field; with no focus
the first listed field, and the snapshot lists no password fields), key (Enter / Escape...), scroll (the snapshot's scroll action),
back, forward, tab (Target.getTargets / attachToTarget / activateTarget), read, stop, help; task and refused answer `ok: False`. A click
whose label `policy.needs_approval` flags is held as `pending` and runs only on "approve" (re-checked fresh), "reject" or "stop" drops
it. README "Voice" (the session paragraph).
**Verified**: gate V4 passes (scroll down, click Next page, go back each under 1 s on listing1.html; "RAM kits" read back);
`tests/test_voice_session.py` (5 tests, headless Edge/Chrome over CDP: a commitment click waits for approve and reject drops it,
nothing typed into a password field while the email field takes the words, search box + Enter lands on results.html?q=, prefix
labels and nth links, a private address refused unless allowed, task / refused / tab / help answers); mutation check:
`needs_approval` bypassed -> the commitment test fails; restored. Ruff clean over src, tests, readiness, scripts.
**Measured**: the three gate commands took well under a second each on the fixture page (the whole gate runs in ~4 s including the
browser start); $0 spent - no model is called anywhere in a voice session.
**Not done by design**: a label is matched only against what the snapshot lists (no OCR, no fuzzy spelling); a "search" on a page
without a search box goes to DuckDuckGo; tabs are the browser's page targets, not Fleet's profiles; no trust rules ("always allow")
for spoken clicks yet - every commitment asks. V2 (the API) and V3 (the panel) follow.

## Loop run 32 - V2: voice endpoints (2026-10-19, voice session, branch claude/loop-voice-1362e9)
The API gets a voice surface: a task from transcribed words, a live session on a local profile, commands against it.
**Built**: `src/fleet/api.py` (the voice block only, at the end of `_routes`, plus three body models with defaults and one import):
`POST /api/voice/task {"text"}` -> 201 `{"task_id", "prompt", "said"}` ("new task X" / "zadanie X" means X, any other sentence is the
task itself, a refused utterance is 400, under three characters is 400; the task is made through the same `create_task` as
POST /api/tasks with TaskIn's defaults); `POST /api/voice/session {"url", "profile_id"?}` -> 201 `{"session_id", "profile_id", "url"}`:
the profile is the local one named "Voice" (created on first use with `profiles.add_local`, headless false, general_use false so no
worker ever leases it; or the one named), opened visible through `browsers.open`, then `voice.Live.connect` (a CDP connection of its own
and a `Session`; BrowserError -> 502, a private address -> 400, CDP trouble -> 502); sessions live in the module dict
`api.voice_sessions`; `POST /api/voice/command {"session_id", "text"}` -> 200 `{"command", "result"}`: refused -> 400 before any lookup
and nothing typed, not understood -> 400 "I didn't understand that.", a "new task" needs no session, otherwise the session runs it
(unknown session -> 404); `DELETE /api/voice/session/{id}` closes the tab. `voice.Live` in `src/fleet/voice.py`. README "Voice".
The gate test's assert read `prompt` at the top level of GET /api/tasks/{id}, which has always answered `{"task", "subtasks"}` (the panel
reads it that way): the assert now reads `["task"]["prompt"]` - the same check against the route's real shape, not a looser one.
**Verified**: gate V2 (3 tests); `tests/test_voice_api.py` (6 tests with a stand-in browser and Live: "nowe zadanie" words and a plain
sentence become tasks, a card number in a task is 400 and never echoed, a session opens a visible tab on a "Voice" profile that is
local / not general use / not headless and is reused by the next session, commands run there and answer the command and the result,
refused and misunderstood words never reach the page (also without a session), a spoken task in the command route needs no session,
a private page is 400 and no browser is 502, the three routes refuse a missing header and a foreign origin); `tests/test_api_sweep.py`
green with the four new routes (the body models have defaults); `test_api_security.py`, `test_local_browser.py` green. Mutation
check: the refused branch of /api/voice/command removed -> the gate's refusal test fails; restored. Ruff clean.
**Measured**: 16 tests in ~10 s against the test Postgres; $0 - no model call anywhere in the voice routes.
**Not done by design**: sessions are a dict in one process (a restart forgets them; the panel opens a new one); no listing route; the
profile window is opened visible so the operator sees what they talk to, which means a headless-only machine answers 502 from
`browsers.open` exactly as the Profiles page's "Open" would. V3 (the panel) follows.

## Loop run 33 - V3: push-to-talk in the panel (2026-10-19, voice session, branch claude/loop-voice-1362e9)
The last of the four voice gates: the panel listens and speaks, with no model and no cost.
**Built**: `web/src/Voice.tsx` - a `Voice` component in the sidebar (mounted in `App.tsx` above the system status, fed the Inbox's
waiting plans): push-to-talk through `SpeechRecognition` / `webkitSpeechRecognition` (the types the DOM lib lacks are declared in the
file; a browser without it gets a disabled button that says "Voice needs Chrome or Edge"), one utterance per press, PL / EN language
toggle and a readback mute, both remembered in localStorage. The words go to `api.voiceCommand` (POST /api/voice/command, named in
full - `request()` in `api.ts` now takes a path that already starts with /api/ and sends the same panel header and token); a 404 "no
session" with "open <site>" opens the session there (POST /api/voice/session), otherwise the panel says "No page is open yet"; a 400
(refused, not understood) is read back as the server said it; `task_id` in a result opens the task page; `needs_approval` arms a
pending click, so the next "approve" goes to the page, while "approve" / "reject" with a plan waiting in the Inbox and no pending
click calls `approveTask` / `cancelTask` on the first plan. Readback with `speechSynthesis` (`SpeechSynthesisUtterance`, the chosen
language, 400 characters at most): the answer of every command, and from the event stream a plan waiting ("say approve or reject"), a
`human.request`, and a task done or failed. `VoiceResult` and the four voice methods in `api.ts`; `.voice` styles (a pulse while
listening). README "Voice" (the panel paragraph).
**Verified**: gate V3 (2 tests: the component listens with SpeechRecognition and speaks with speechSynthesis, calls /api/voice/, is
mounted in App.tsx; the built panel's JS carries both); `tests/test_panel_voice.py` (5 static tests: listens and speaks with the
browser in both languages, the words only ever go through api.ts to the three voice endpoints and the two Inbox calls, request()
keeps the panel header for a path named in full, approve / reject answer the Inbox unless a page click waits, the component is in the
sidebar once); mutation check: the `<Voice />` mount removed from App.tsx -> the gate's mount test fails; restored. `npm run build`
clean (tsc + vite, 5 s). Ruff clean. The full suite ran once at the end of the four gates (see the report).
**Measured**: the built bundle grew by about 6 kB gzipped; $0 - recognition and synthesis are the browser's.
**Not done by design**: how it feels is the operator's judgement (H3): nothing here was heard through a real microphone, only built
and read; recognition quality is the browser's (Chrome / Edge send audio to their own speech service - the operator should know that
the words, not the page, leave the machine); no wake word, no continuous listening (one press, one utterance); "approve" answers the
first waiting plan only.

## Loop run 34 - K5: the second visit is cheaper (2026-10-19)
The gate was failing on a hole in the fixture shop, not on Fleet: `results.html` has always linked its two hits to
`product.html?id=N`, and no such page existed in either fixture root. So the first visit typed "raspberry pi 5", clicked
"Raspberry Pi 5 8GB - 399 zl", landed on the http server's 404 page - which offers nothing to click - and Jev correctly
answered "no supported action that makes progress": status blocked, `out.finished` false, and the test stopped before the
second, guided visit was ever measured. The smallest honest change was to add the page the shop already pointed at.
**Built**: `tests/fixtures/fast/product.html` and `readiness/fixtures/product.html` (the two fixture roots that serve
`results.html`, for the `site` and `rsite` fixtures): a plain product detail page that reads `?id=` the same way
`results.html` reads `?q=` - id 1 is "Raspberry Pi 5 8GB / 399 zl", id 2 "Raspberry Pi 5 4GB / 299 zl", with the name in
the title and the `h1`, a price line, a stock line and one "Back to results" link. No cart or buy control on purpose: the
goal of these gates is to *open* the top hit, and a commitment button would invite a click (and an approval round) that the
goal never asked for. No source, prompt, migration or doc change - the engine's behaviour on a dead end was right.
**Verified**: `readiness/gates/test_live.py::test_k5_second_visit_is_cheaper` passes (first visit done, second visit done);
`readiness/gates/test_a2_benchmark.py::test_search_open_extract` (the scripted walk of the same three pages) still green;
full suite `python -m pytest -q` 660 passed in 15:40 against a fresh test Postgres; ruff clean over src, tests, readiness,
scripts. Mutation check: `tests/fixtures/fast/product.html` moved away -> K5 fails again with exactly the old reason
("Jev found no supported action that makes progress"); restored.
**Measured**: first visit $0.000696 (4 model calls: one Jev decision per page plus one field text), second visit with the
paraphrased goal and the first run's trace as the guide $0.000062 - 8.9% of the first, against the gate's 30% ceiling; both
runs finish in about 3 s each (the gate as a whole, browser start included, 6.4 s). Two live runs of the gate plus one
mutation run cost about $0.0015 in total.
**Also**: the test Postgres cluster in `readiness/.pg` could no longer start - it had been killed uncleanly at 13:23, and on
Windows its crash recovery can never finish because `pg_ctl` holds `<pgdata>/log` open while `SyncDataDirectory` tries to
read it ("could not open file ./log: sharing violation"). The wedged processes were stopped and the throwaway cluster
directory removed, so `pg.start` initdb'd a fresh one; nothing of value lives there. The readiness runner would have hit
the same wall on its next start.
**Not done**: R4 (the same first run, replay at <= 10% instead of 30%) was failing for this very reason and should pass now,
but it was not run this iteration - one gate per iteration, and the loop's own readiness run will say. The product page is
static: no variants, no stock states, nothing for a gate about buying to lean on yet.

## Loop run 35 - A7: the live benchmark (2026-10-19)
A7 needs 3 of 3 (80% of three tasks), and it was failing on two independent causes, both found by driving the three
tasks by hand and printing the engine's own trace.
**Cause 1, deterministic.** A control cut by the edge of the viewport was dropped from the observation altogether: both
the page reader and the pre-input target resolution aimed at the element's *geometric centre* and required that raw
centre to be inside the window, so an element straddling the fold - on screen, clickable, half of it in plain sight -
reached Jev as nothing at all. On `https://www.wikipedia.org` in the gate's 754x487 headless window the portal's search
field sits at y 472-516: fifteen pixels show, the centre is seven pixels below the fold. Jev was handed the ten language
links and nothing else, answered BLOCKED, blind-scrolled past the whole form (one screen is 560 px, the page is 1409
tall, so after two scrolls no `scroll_down` was left to offer) and the task ended `blocked` on every run - $0.000325 and
two wasted screens, byte-identical at 13:22 and 14:16.
**Cause 2, a coin flip.** On `quotes.toscrape.com` the pager is only ever visible on the fourth screen (its rect is
y -11..+23 at scroll 1680; one scroll further it is above the window and gone for good). About half the time Jev clicked
an "(about)" author link on the second screen instead, landed on `/author/Albert-Einstein/`, read that page out and then
had nowhere to go: the engine ended the step in the dead end, with the pager still sitting on the listing it had left.
**Built**: two changes, both in code deciding rather than a prompt nudge.
- `src/fleet/fastbrowser/snapshot.js`: a new `onscreen(r,ox,oy,win)` intersects the element's frame-local rect with its
  own frame's window and with the window around it (whose edges sit at `-ox` / `innerWidth-ox` in frame coordinates) and
  offers the action when at least `MIN_VISIBLE` = 4 px show in each direction - `Math.min(4, extent)`, so a small element
  entirely on screen is never dropped for its size. `src/fleet/fastbrowser/page.py` (`TARGET_JS`): the same clip, so
  input aims at the centre of the part that is on screen - the point the observation promised - and the existing hit test
  (`e.contains(doc.elementFromPoint(x,y))`) still has to prove that point belongs to the element before one event is
  sent. A straddling control is offered, never clicked blind. The enclosing-frame walk is untouched; a fully visible
  element's intersection is its own rect, so nothing about it moves.
- `src/fleet/fastbrowser/engine.py`: a bounded back-out. When Jev reports BLOCKED and there is nothing left to read,
  the engine steps back instead of ending the step - `MAX_BACK_OUTS` = 2 - and `depth` counts the pages it navigated
  into itself (incremented when a step changes the URL, decremented inside `go_back`, so the domain-strike path and
  Jev's own "back" keep it honest). `depth > 0` is what makes it safe: without it the back-out walked out of the run's
  own first page and the step died with "Inspected target navigated or closed". Jev is asked about the current page
  only, so "the page before this one still had the pager" is the one answer it cannot give; the engine supplies it.
  The step is recorded as operation `BACK_OUT` and appears in the trace as a `back`, so a replay repeats it.
- `README.md` (Fast browser), same commit: the two new behaviours in the paragraph that already described the
  blind scroll. `tests/conftest.py`: `scripted` now accepts `"BLOCKED"` as well as `"DONE"`.
**Verified**: `readiness/gates/test_live.py::test_a7_live_benchmark` passes 3/3, four runs in a row (12/12 tasks), where
it was 1/3. Two new tests in `tests/test_fastbrowser_engine.py` pin the back-out deterministically against the local
fixtures, with Jev scripted: `test_a_wrong_turn_the_engine_took_is_walked_back_out_of` (click the first hit on
`results.html`, BLOCKED on the product page, and the engine is back on the results page and takes the second hit -
trace `click, back, click`, operations `CLICK, BACK_OUT, CLICK`, the four pages visited asserted by name) and
`test_the_start_page_is_handed_over_rather_than_backed_out_of` (BLOCKED where the engine has not moved is still
`blocked`, with the old reason). The whole live gate file - A7, B3, B4, R4, K5, C5 - and the seven local browser gates
that share the page reader (A1, A2, A3 frames, A4 upload, A6 new tab, B2 macro, R3 self-healing): 23 passed. Full suite
`python -m pytest -q` 662 passed in 15:53 against a fresh test Postgres (660 before, plus the two new tests). Ruff clean
over src, tests, readiness, scripts. `web/` untouched, so no `npm run build`. Two mutation checks: the raw-centre test
put back inside `onscreen` -> A7 fails with exactly the old shape (`2/3 live tasks passed`, Wikipedia `blocked`);
`MAX_BACK_OUTS` = 0 -> the new back-out test fails and the start-page one still passes. Both restored.
**Measured**: A7 costs $0.00255-$0.00320 for the three tasks in 16-20 s, against the $0.25 run budget. Wikipedia
$0.001472-$0.001739 (four model rounds to the article instead of one dead end plus two blind scrolls); Mystery
$0.000507 unchanged; quotes $0.000573 when Jev reads straight down to the pager and $0.000976 when it takes the author
detour and the back-out recovers - three of the last four runs took that detour and all three still finished. The
wikipedia.org observation grew from 12 actions to 15 (the search field as `fill`, its "Open" twin, the Search button).
About $0.02 of live calls this iteration, diagnosis included.
**Not done**: the blind scroll is still a fixed 560 px jump, so a control that falls between two screens can still be
stepped over on a page tall enough to offer another scroll - what went away is the case where the control was on the
*first* screen all along. The back-out does not remember which branch it came out of: Jev may click the same wrong link
again, and then only the second back-out and the reading that follows it save the step. Nothing was done about Jev's
choice itself (an "(about)" author link for "go to the next page") - that would be a prompt change, and this iteration
had no cheap way to show it helps rather than moves the failure somewhere else.


## Loop run 36 - K2: the base answers questions (2026-10-19)

**Plan**: both answers the experience base gives are about *the latest* row - `best_path` returns the actions of the
latest successful run with a similar goal, `site_notes` quotes what the last run that did not finish said - but both
queries ordered by `created_at` alone, and `created_at` comes from the clock. The clock has a step: on Windows
`datetime.now()` moves in 15.625 ms jumps (measured: 2000 out of 2000 adjacent calls returned the same value), so two
steps written in the same tick carry the same timestamp to the microsecond and `order by created_at desc` may return
them in either order. That is exactly what the K2 gate does - three rows for `news.test` inserted back to back - and
when Postgres handed them back in insertion order, `site_notes` quoted the second-to-last failure ("This page asks for
a login") instead of the last one ("The operator didn't approve "Post""), so the gate's `"approve" in notes` assert
failed. The smallest fix is to give the base a monotonic insertion order and sort by it after the timestamp.

**Built**:
- `src/fleet/migrations/021_experience_order.sql`: `alter table experience add column seq bigserial` - the order the
  rows were written in - and the domain index becomes `(domain, created_at desc, seq desc)` so the new sort is the
  index's own order (the old `experience_domain_idx` is dropped).
- `src/fleet/experience.py`: `best_path` and `site_notes` order by `created_at desc, seq desc`. The module docstring
  says what "the latest run" means now and why the clock alone cannot say it.
- `src/fleet/experience_api.py`: the operator's listing had the same tie, broken by `id` - a random uuid, stable but
  arbitrary, so a tie was shown in an order that had nothing to do with time. It now uses `seq desc` as well, which is
  what K4's "newest first" says. `seq` is not in `COLUMNS`, so nothing new goes out over the API.

**Verified**: `readiness/gates/test_k2_experience_queries.py` 2 passed (it was 1/2); K1, K4 and `tests/test_experience.py`
pass with it, 17 together. One new test, `tests/test_experience.py::test_two_runs_in_the_same_clock_tick_still_have_a_latest_one`,
pins the behaviour deterministically where the gate only caught it by luck: four rows inserted with one `now()` value
(asserted to be a single distinct `created_at`), and both questions have to read the row written last. Mutation check:
`order by created_at desc, seq asc` - "the oldest of the tie" - fails the new test on both answers (`best_path` returns
the first path, `site_notes` quotes the login wall), restored after. Note that simply deleting `, seq desc` does *not*
fail the test any more, because the new index already carries that order and the planner uses it; the explicit clause is
what makes it a promise rather than a plan detail. Ruff clean over src, tests, readiness, scripts. `web/` untouched.

**Measured**: no model calls, so no cost. The clock's granularity: 15.625 ms, 2000/2000 adjacent `datetime.now(UTC)`
pairs equal. The gate file runs in 1.1 s, the four experience files in 12.3 s. One migration, one new column, one index
swapped for another with one more key.

**Not done**: the gate's three rows still share a timestamp - the fix makes the order deterministic, it does not make
the clock finer, so anything that reads experience by `created_at` alone (an operator's SQL, a future report) sees the
same tie. `seq` is a bigserial, so it is the write order of the row, not of the run: two steps that finished in one tick
are ordered by which worker got to the insert first. Good enough for "the latest", wrong for "which ran first".
