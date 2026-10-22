# Final autonomous report — Fleet

Eight improvement passes plus one architecture review, each committed separately (`git log`).
Verification at the end: **57 tests pass** (`pytest`, real Postgres), `ruff` clean, panel builds with strict
TypeScript, every user-facing flow below was also driven live in a browser.

## 1. What was improved

| Pass | Problem | Result |
|---|---|---|
| 1 | Local API: path traversal, DNS rebinding, CSRF | Closed; attack tests + live probes |
| 2 | Product needed a Claude key; user wants OpenRouter + key storage | All models via OpenRouter; encrypted vault; Settings page; video worker |
| 3 | Plans ran on your accounts without your OK; crash could repeat an action | Plan review; no auto-retry after irreversible actions; idempotent answers |
| 4 | Unnamed browser steps could use any account profile | Only "general browsing" profiles unless the plan names one |
| 5 | Failed task = retype everything | Run again |
| 6 | Nothing could repeat or run on a schedule | Saved automations + schedules |
| — | Architecture review | Shared test fixtures, lint clean, docs rewritten, FUTURE_IDEAS.md |
| 7 | Setting the access token broke the panel; screenshots unprotected | Lock screen; token guards `/files` |
| 8 | Raw traceback when Postgres is down / login wrong | One readable line, password never printed |

## 2. Architecture observations

- **Postgres is the single source of truth**: queue (`FOR UPDATE SKIP LOCKED`), leases, schedules, audit log,
  NOTIFY → SSE. That's why crashes, restarts, several workers and several conductors are all safe. Keep it.
- **AI vs code boundary is explicit**: AI plans, extracts, researches, drives the browser, writes answers.
  Code decides permissions (plan review rule, profile eligibility, approvals, budget), state transitions,
  schedules, retries and validation (Pydantic on every structured answer).
- **An automation is just a saved list of steps** replayed as an ordinary task — no second execution engine.
- Worker kinds share one contract (`RunContext` → `ExecResult`); adding a kind = one file + planner prompt line.

## 3. Bugs fixed

- Panel route served arbitrary files (incl. `.env`) via `../`.
- API answered any `Host`; body-less POSTs were cross-site callable.
- Retries of a browser subtask were pinned to the first profile; a broken profile hung the task forever.
- Cost wasn't recorded when a call reported spend but no tokens.
- `_promote` queued work for tasks in any status.
- Double click could answer a human request twice.
- A missing API key would open an AdsPower window before failing.
- Test database kept a stale schema after a migration edit (11 false failures) → recreated every run.

## 4. Reliability improvements

- Account/credit/model errors (401/402/404) fail fast with advice; 408/429/5xx/timeouts retry with backoff.
- Malformed AI output gets one repair round, then a readable failure.
- Subtasks that did something irreversible are never re-run automatically.
- Automations: one run per slot even with concurrent conductors; runs never overlap; missed slots not replayed;
  events emitted after commit.
- Master-key problems surface as "paste the key again", not endless retries.

## 5. UX improvements

- First-run banner + disabled Run until an OpenRouter key exists; Settings with key check (credits left).
- Model picker per role from the live OpenRouter catalog, with price, context and capability checks.
- Plan review card (what each step will do, on which profile); plans in the Inbox with a badge.
- "Run again", "Save as automation", Automations page with schedule editor, approval toggle, last run.
- Activity feed shows the actions a browser step took; clearer failure cards; readable startup errors.

## 6. Security issues addressed

Path traversal, DNS rebinding, CSRF, unusable/incomplete token protection, API keys at rest (AES-256-GCM,
master key outside the DB, write-only API, log scrubbing), prompt-injection reach into unrelated logins
(general-use profiles), unapproved account actions (plan review), repeated irreversible actions.

**Found on this machine, not changed (your call):** the local Postgres 17 accepts connections without checking
passwords (pg_hba `trust`), so any local program can open any database as superuser. Checking the startup
error created the (empty) `fleet` database there — the one Fleet uses by default.

## 7. Remaining risks

- **No real model call or real AdsPower run has been made** (no OpenRouter key on the machine, AdsPower closed).
  Everything around them is tested with fakes; the first real runs may need prompt or parameter tuning,
  especially browser-use with OpenRouter structured outputs.
- The captcha detector is heuristic; side-effect tracking only covers actions the agent declares.
- Events/screenshots grow without bound (no retention yet).
- Single-user: no accounts or tenants.

## 8. Recommended next steps

1. Paste an OpenRouter key, run three real prompts (research, fetch fan-out, one browser task on a
   general-browsing profile) and fix whatever the real models do differently.
2. Save one of them as an automation with a schedule and let it run for a few days.
3. Then decide between event triggers (webhooks) and recipe replay for browser steps (FUTURE_IDEAS.md).

## 9. Intentionally not implemented

Event triggers, editing saved steps, recipe replay, custom API tools for workers, per-subtask retry inside a
finished task, cost estimates on plan review, tenants/accounts, retention, Docker/CI. Reasons in FUTURE_IDEAS.md.
Also not built, by the rules agreed earlier: personas, bulk accounts, captcha solving, proxy rotation.

## 10. Highest-leverage future improvements

1. A real end-to-end run with a key and AdsPower (turns "tested with fakes" into "works").
2. Recipe replay for automation browser steps (cheaper, faster, more deterministic scheduled runs).
3. Webhook triggers per automation ("when X happens").
4. Retention + the pending-subtask index before data grows.
