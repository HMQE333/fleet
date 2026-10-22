# Roadmap: more automation, more scraping, logins — with the operator in control

Goal (from the operator, 2026-10-02): more automation and scraping potential; Fleet can log in or
register when a task needs it; the operator can still handle every Inbox item themselves if they choose.

Each scheduled run picks the highest-impact unfinished slice below, does ONE verified improvement,
updates the status column, and commits to `master`.

## Hard rules (a run must never relax these; they are also in RULES.md)

1. **Real accounts only.** Registrations create one account for the operator, from identity details the
   operator supplied, one at a time and each approved in the Inbox. Never bulk sign-ups, fake identities,
   personas, or accounts that pose as independent people. Never fake engagement.
2. **A human handles challenges.** Captchas, "verify you are human", email/SMS verification codes and 2FA
   go to the operator. Never solve, bypass or farm them out. No stealth/anti-detection tricks, no proxy rotation.
3. **No model ever sees a password.** Credentials are typed by Fleet's code from the encrypted store; they are
   never sent to a model, never put in prompts, events, traces, screenshots' text, logs or API responses.
   Passwords stay write-only in the API except ones Fleet generated for an account Fleet registered (the
   operator owns that account and must be able to read it; each reveal is logged).
4. **The operator controls every Inbox item.** Defaults ask. "Automatic" is opt-in per saved login or per rule,
   never global, never for payments, registrations or captchas. The operator can always choose "I'll do it myself".
5. **Be a polite scraper.** robots.txt honoured, per-domain pacing, no hammering, stop on blocks. A site that
   blocks automation is handled with the operator's own logged-in browser profile, not with evasion.
6. **Irreversible actions still need approval; payments always.**
7. Never edit an applied migration (add a new one). Keep tests green, ruff clean, panel building.

## How to test

- Throwaway Postgres: `uv run --no-project --python 3.12 --with pgserver python pgtest.py <fresh folder>`
  (in the session scratchpad; prints a URI), then
  `FLEET_TEST_DATABASE_URL=postgresql://postgres@127.0.0.1:<port>/fleet_test uv run pytest -q`
- Lint: `uvx ruff check src tests --select E,F,W,B,UP,SIM --ignore E501,SIM105,SIM108,B008,UP017`
- Panel: `cd web && npm run build`; live check with `preview_start` using a `fleet-test` launch config
  pointed at a throwaway database (remove the config afterwards).
- Write patch scripts with the Write tool, not bash heredocs (`\n` inside heredocs has broken files before).
- Real model calls only for cheap verification (at most about $0.30 per run, log it in the progress file);
  everything else uses stand-ins. Don't restart the operator's server while tasks are active.

## Stages

| # | Stage | Status |
|---|---|---|
| S2a | **Saved logins**: encrypted per-site (optionally per-profile) logins; Fleet's code logs in when a task hits a login form; Inbox card asks first unless the operator marked that login automatic; one attempt per run (no lockouts) | **done (run 1)**: encrypted store, login detection (login vs sign-up vs change-password), code-typed login in both browser workers, Inbox card `login_saved`, per-login automatic switch, Settings → Site logins, 18 tests. Follow-ups: two-step logins and frames (S7), reveal for Fleet-generated passwords (with S3) |
| S1 | **Structured scraping**: extraction to a schema (JSON), follow pagination, many items per task, CSV/JSON download in the panel | **done (run 2)**: `scrape` worker (recipe learned once, applied by code), pagination, dedupe, caps, partial results, data table + CSV/JSON in the panel, recipe reuse in automations, 31 tests, real-site check ($0.02 for 200 items). Follow-ups: task-level page/item limits, following links into detail pages, JS-built lists via browser_fast, results as input to later steps |
| S3 | **Registration** with the operator's identity details: each sign-up approved in the Inbox, generated password stored and revealable by the operator, email/captcha verification handed to the human | **done (run 3)**: task option (off by default, not inherited by automations), Settings → Your details, form reader (sign-up vs login vs change-password; unknown required fields → operator signs up; newsletter never ticked), Inbox `register` + `verify_email` cards, one per site/profile + daily limit (re-checked after approval), cross-site forms refused, generated password saved before submit / kept as unconfirmed if sent but not confirmed / revealable + logged, side-effects flag, both browser workers, planner + agent prompts, 33 tests. Follow-ups: sign-ups with email-code steps or multi-page forms (S7), per-site "never register here" list (S4) |
| S4 | **Inbox control**: per-task "autonomy" level and per-site/per-action trust rules ("always allow X on site Y"), pause/resume/take over, clearer Inbox grouping | **slice 1 done (run 4): trust rules** — "Approve and always allow" on fast-browser approval cards (exact label + exact host + this profile; never payments, deleting, cancelling, terms, sign-ups, generic labels), used rules logged and still mark the step as acted, Settings → Trust rules (list, remove), no way to create a rule except from an answered card, 47 tests. **Slice 2 done (run 7): pause / resume a task and take over a running browser step** (migration 014, claim queries skip paused tasks, `take_over` request read once at the worker's next step in both browser workers, `takeover` Inbox card, Pause/Resume and Take over buttons, paused pill/banner, 8 tests). **Slice 3 done (run 11): the Inbox groups cards by task and answers them together** (explicit ids, eligible kinds only, payments never, 23 tests). **Left**: rules for the full browser agent (its approvals are free text, so there is nothing exact to match) |
| S5 | **Crawl worker**: map/crawl a site section with pacing, dedupe, resume after restart | **slice 1 done (run 8): item pages** - a scrape step opens each item's own page and adds its values as extra columns: the list recipe may name the link field and what is wanted, the model learns an item-page recipe once from a real sample (checked by code, one repair round), same-site only, polite, `scrape_max_details` cap (30), stops after 3 refusals, keeps list data when item pages fail, saved automations reuse the recipe for $0 (relearned if it stops fitting), change reports ignore columns that were only partly read, 14 tests with a stand-in model. **Crawl done (run 10)**: a `crawl` worker kind (migration 016): one site section by code, no model, limits on pages / depth / time, trap guard, resume after a restart (progress saved every 10 pages), results in `scrape_results` so previews, CSV/JSON, change monitoring and Updates apply, 24 tests. **Sitemaps done (run 12)**: a crawl also reads the site's sitemap.xml (and a few child files of a sitemap index) for pages of the section nothing links to. **Left**: JS-built item pages, per-task limits (the global ones are editable under Settings -> Limits) |
| S6 | **Change monitoring**: compare scraped data between scheduled runs and report what changed | **slice 1 done (run 5)**: every scrape step of a saved automation is compared with the previous run of the same step (the first run: with the task it was saved from): new / removed / edited rows matched by a unique id or link column (else by whole row), whitespace ignored, no new/removed claims when either run stopped early, different columns reported as not comparable; summary at the top of the step result, `scrape.changes` event, counts pill + lists in the panel, stored in `scrape_results.changes`, 15 tests. **Updates feed done (run 9)**: scrapes of saved automations that found changes appear in the Inbox under Updates (counts, link to the details, mark one / all as seen, counted in the Inbox badge), quiet runs never do, 6 tests. **Left**: email alerts (a webhook exists: Settings -> Notifications; thresholds exist: Watches), history across more than two runs, JS-built lists |
| S7 | Two-step logins (username page, then password page) and logins inside frames | **slice 1 done (run 6): two-step logins** - username page detected in code (one identifier field, nothing else to fill in, sign-in context, no sign-up/newsletter signals), only when a login is saved for the site, then the password page through the usual login code; works for a page that swaps its form and for two real pages; both browser workers; Inbox card and automatic mode unchanged; one attempt per run; 31 tests. **Left**: logins inside iframes (embedded sign-in widgets), providers that add a third step, sites where the username page has no sign-in wording |

## Remaining work (plan of 2026-10-08, executed in this order; each item = tests + mutation checks + live check + commit)

| # | Item | Status |
|---|---|---|
| P1 | **API security sweep**: a test that walks every route: mutating routes refuse a request without the panel header, every route refuses a request without the token when one is set; guards routes added later | **done (run 15)**: 7 tests walk all ~50 routes (changes need the panel header and a same-site origin; with a token every route and /files refuse requests without it; nothing outside /api); the generated /docs, /redoc and /openapi.json pages are switched off. Every route already passed. |
| P2 | **Price / threshold watches** on saved scrapes ("tell me when price <= X"): parsed numbers, only newly true matches, shown under Updates | **done (run 16)**: `watches.py` + migration 017, up to 20 rules per automation (column, <, <=, >, >=, number) checked against the step's real columns, numbers read in any local format, alerts only for rows that just crossed the line (matched by a unique column, else as whole rows), kept in `scrape_results.alerts`, first in the step's result and under Updates (alerts alone are news), Automations -> Watches and the Inbox card, 33 tests |
| P3 | **Retry one failed step** of a finished task instead of re-running the whole task | **done (run 17)**: `conductor.retry_subtask` + `POST /api/subtasks/{id}/retry` + a Retry button: the failed step and the steps skipped because of it (transitively) start over, everything else stays, the task returns to running and is reviewed again; refused for non-failed steps, unfinished or stopped tasks, earlier rounds and steps with irreversible side effects; 5 tests |
| P4 | **Retention**: prune old events, screenshots and histories (setting in Limits), never touching running tasks or saved automations | **done (run 18)**: `retention.py`; `retention_days` in Limits (0 = keep, default); finished tasks older than that lose screenshots, histories and step/log events only (answers, scraped data, automations and the audit trail stay); automatic every ~6 h from the conductor loop (throttled), "Clean up now" and a storage readout in Settings; 10 tests |
| P5 | **S4 leftovers**: per-task "ignore my trust rules"; per-site "never register here" list | **done (run 19)**: `tasks.use_trust_rules` (task option, copied by run again and saved automations, default on) and `no_register_domains` (Settings list, subdomains covered, look-alikes not, refused before the operator is asked); migration 018; 5 tests |
| P6 | **Opt-in notifications**: a webhook to an address the operator enters (public https only, short text, never page data or secrets), off by default | **done (run 20)**: `notify.py`; address in the vault (never returned; only the host shown), https + public + checked again at send time, no redirects, secrets scrubbed, text capped, 20/hour with a pause notice, failures logged without the address; three triggers (needs you / updates / finished), json or plain; Settings -> Notifications with a test button; 22 tests |
| P7 | **First real runs checklist** (`FIRST_REAL_RUNS.md`): cheap, exact checks for what needs OpenRouter credits or a real AdsPower profile | **done (run 20)**: nine steps from a $0.02 answer to a sign-up, each with what good looks like and what to write down |
| P8 | **Consistency pass**: README / RULES / ROADMAP / FUTURE_IDEAS match the code | **done (run 21)**: README worker table + diagram, doc index and leftovers fixed; FUTURE_IDEAS cleared of what is built; `tests/test_docs.py` fails when a doc names a setting or a file that doesn't exist (it found FLEET_RETENTION_DAYS missing from .env.example) |

Deliberately not planned: login inside cross-origin iframes (typing credentials into a third party's embedded frame is the phishing
shape; same-origin frames are rare), teach-by-video and voice mode (need real model credits to judge), accounts / tenants (cloud stage),
a browser fallback for JavaScript-built lists (needs real runs to design).

Needs YOUR credits or a real AdsPower browser (build is done, judgement is not): the planner and recipe-learning prompts with a real model
(does it choose `scrape` / `crawl`, set the item-page link field, write good selectors), the full browser agent hooks (take over, payment
flag), saved logins / two-step logins / registration on a real site, change monitoring over several real days. P7 turns these into a
checklist.

## Decisions already made

- Logins belong to **profiles** (each AdsPower profile is one real identity). A login with no profile applies to any
  profile. Matching: the profile's own login for the site first, then the global one.
- Login pages are recognised in code (exactly one visible password field, `current-password` or no sign-up
  signals); registration forms (`new-password`, a confirm field, "sign up"/"create account") are never auto-filled
  with a saved login.

## RULES.md section 3 rewrite: apply it only when S2a's login code and tests exist

Replace "Fleet never handles passwords" with "AI models never see passwords": the operator may save a login for a
site; when a task reaches that site's login form, Fleet's code (not a model) types it. It asks in the Inbox first
unless that login is marked automatic ("I'll do it myself" is always an option); one automatic attempt per run
and site (no lockouts); a saved login is only typed into a login form, never a sign-up or change-password form;
Fleet never types 2FA codes, card numbers or government IDs. Enforced by: `site_accounts.py`, `saved_login.py`,
`fastbrowser/login.py`, `tests/test_site_logins.py`.

## Design notes for the remaining S2a work

- Login detection in page JS: exactly one visible password field; `autocomplete=new-password`, a confirm field, or
  a sign-up submit button/heading/URL means "register" and is never filled. Username field = last visible text
  input before the password field in the same form. Submit with `submit.click()` if there is a submit button,
  else Enter. Focus the field and use `Input.insertText` (no coordinates needed).
- `SavedLogin.try_login(send, url)` shared by both workers; `send(method, params)` adapts the fast engine's page and
  browser-use's CDP session. Success = the login form is gone within about 4 seconds; failure marks
  `last_failed_at` and falls back to the existing human hand-off card.
- Add a 422 handler that strips `input` from validation errors so a rejected password is never echoed back.
- Login steps are not recorded in browser traces (login is state, not path).
