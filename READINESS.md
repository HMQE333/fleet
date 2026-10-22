# When is Fleet ready?

Fleet is ready when it is an agentic browser that does what the operator asks on the web, with fewer limits than a
browser's built-in AI, for less money, and can be driven by voice. This file turns that into gates a program can check.
`python -m readiness` checks them and writes `readiness/scorecard.json` and `READINESS_STATUS.md`; the improvement loop
(`LOOP.md`) works through the failing gates in priority order until every gate passes and the operator has signed off.

## The six promises

1. **Any task.** A browser step can do what a person can do in a page: search, forms, logins the operator saved,
   pagination, frames, uploads, editors, new tabs, on the operator's own logged-in profiles, in parallel, on a schedule.
   Fewer refusals and fewer dead ends than Chrome's or Edge's built-in assistant, which is confined to one tab, one
   session, no memory of how it did it last time.
2. **Brain and hands.** A capable model (the brain: Claude Opus / Sonnet, through OpenRouter or the Anthropic API) decides
   *how* once: a plan, a recipe, a recorded path. Cheap hands (Jev decisions at fractions of a cent, a small model for
   typed values, or plain code) execute it. The brain is never in the loop for a step the hands can do alone.
3. **Record and replay.** Every browser step that worked is stored as a replayable path. A task done correctly once is an
   automation: the next run follows the path with no model call where the page still matches, one cheap decision where it
   drifted, and the path is updated so the drift is paid for once. The full agent is the last resort, never the default.
4. **Learns from every action.** Fleet keeps an experience base: every browser step writes down where it was (the site),
   what it was asked, what it did (the actions), how it ended (done, stuck, rejected, blocked), what it cost and how long
   it took. From that base Fleet knows a site before it goes back: the path that worked for a goal like this one (even
   without a saved automation), what failed there and why, where a login is needed. The brain reads it when planning,
   the hands follow it when executing, the operator can read and prune it. The base is why the second visit is cheaper
   than the first and the tenth is nearly free.
5. **Voice.** The operator can speak to Fleet: give a task, steer a live browser ("scroll down", "open the third result",
   "go back", "read it to me"), answer the Inbox ("approve", "stop"). Fast enough to replace the mouse for quick browsing.
6. **Cheaper.** Measured, not promised: a simple task ("open this page, do this one thing") costs cents end to end, because
   the brain is not consulted for what the hands can do alone; a replay costs at most a tenth of the first run; a cheap
   decision costs at most $0.002; the ledger shows what each role spent and what replays saved. Not the
   screenshot-per-step, big-model-per-click economics of the hosted agents.

Plus two things that must stay true while the rest is built: the **foundation** (installs, tests, lint, panel, server)
stays green, and the **rules** (RULES.md: real accounts, humans handle challenges, no model sees a password, approval
before irreversible actions) stay enforced in code.

## Levels: what a gate needs to run

| Level | Needs | Where it runs |
|---|---|---|
| L0 | nothing but this machine: Python, the embedded Postgres (`pgserver`), a headless Chrome or Edge | every run |
| L1 | an API key (`OPENROUTER_API_KEY` and/or `ANTHROPIC_API_KEY` in `.env` or the environment) and a small budget (`FLEET_READINESS_BUDGET_USD`, default $0.25 per run) | when a key is present |
| L2 | live sites over the local Chrome profile (no AdsPower) | when L1 and a browser are present |
| L3 | the operator's judgement, written in `readiness/signoff.json` | by hand |

A gate whose level is unavailable is reported `unavailable`, not failed; the loop still can't declare READY until it has run.

## Gates

`order` is the loop's priority (lower first). `check` names the test or command that decides. Rules gates (S) come first
because the loop must never trade them away; foundation (F) next because every other gate stands on it.

| id | pillar | gate | passes when | level | order |
|---|---|---|---|---|---|
| S1 | rules | safety suites green | `tests/test_api_security.py`, `test_api_sweep.py`, `test_trust.py`, `test_vault.py`, `test_registration.py`, `test_never_register.py`, `test_site_logins.py`, `test_login_steps.py`, `test_webfetch.py`, `test_fastbrowser_unit.py` all pass | L0 | 0 |
| S2 | rules | docs match code | `tests/test_docs.py` passes | L0 | 0 |
| F1 | foundation | installs and imports | `import fleet, fleet.api, fleet.worker` works in the project's venv; `fleet --help` exits 0 | L0 | 1 |
| F2 | foundation | tests green | the whole `tests/` suite passes on a throwaway Postgres (0 failed, 0 errors) | L0 | 1 |
| F3 | foundation | lint clean | `ruff check src tests readiness scripts` reports nothing | L0 | 1 |
| F4 | foundation | panel builds | `cd web && npm run build` succeeds and `web/dist/index.html` exists | L0 | 1 |
| F5 | foundation | server boots | `fleet serve --api-only` on a throwaway database answers `/api/health` with 200 within 30 s | L0 | 1 |
| F6 | foundation | runs without external Postgres | `FLEET_DATABASE_URL=embedded` starts an embedded Postgres under `data/pg` and the server boots on it | L0 | 1 |
| A1 | any task | local Chrome, no AdsPower | a profile of kind `local` (its own user-data-dir, headless or visible) can be opened and driven by both browser workers; `fleet profiles add-local` creates one | L0 | 2 |
| B2 | brain/hands | the brain writes a macro | `fleet.macro`: a step list (navigate, click, fill, select, wait_for, extract, back, scroll, assert_text) with a JSON schema; `macro.validate` rejects bad steps; `macro.run` executes a valid macro in the fast engine with zero model calls | L0 | 2 |
| R3 | record/replay | self-healing replay | replaying a recorded path on a page where ONE label changed finishes with exactly one model decision, and the saved automation's path is updated to the new label | L0 | 2 |
| K1 | experience | every step is written down | after every browser step (both engines, any outcome) a row lands in `experience`: domain, goal, kind, actions taken, outcome, reason, cost, seconds, task and step ids | L0 | 2 |
| K2 | experience | the base answers questions | `experience.best_path(domain, goal)` returns the actions of the latest successful step with a similar goal on that site (and nothing for a different goal); `experience.site_notes(domain)` sums up successes, failures and their reasons, login walls | L0 | 3 |
| K3 | experience | brain and hands use it | a browser step with no saved path gets `best_path` as its guide; the planner's prompt carries `site_notes` for the sites the task names; nothing from the base is ever treated as an instruction | L0 | 3 |
| K4 | experience | the operator can read and prune it | `GET /api/experience?domain=` lists what Fleet learned, `DELETE /api/experience/{id}` forgets one entry, a Knowledge page in the panel shows it per site | L0 | 5 |
| K5 | experience | the second visit is cheaper | live: a paraphrased task on a site Fleet already did costs <= 30% of the first run, with no saved automation involved | L1 | 6 |
| V1 | voice | voice grammar | `fleet.voice.parse` turns Polish and English utterances into commands with >= 95% accuracy on `readiness/benchmarks/voice_utterances.json` | L0 | 3 |
| V2 | voice | voice endpoints | `POST /api/voice/command` runs a command in a live voice session (a local Chrome tab); `POST /api/voice/task` creates a task from transcribed text; both guarded like every other route | L0 | 3 |
| V4 | voice | voice latency | a navigation command (scroll, back, click "label") executes within 1 s of being parsed, measured on a fixture page | L0 | 3 |
| C2 | cost | Claude API as a brain provider | with `ANTHROPIC_API_KEY` set, the planner / smart / fast / browser roles can run on the Anthropic API directly (chosen in Settings → Models), cost accounted from token prices; Jev, Perplexity and Gemini stay on OpenRouter | L0 unit, L1 live | 3 |
| V3 | voice | push-to-talk in the panel | the panel has a microphone button (Web Speech API, no cost) that sends a task or command, and reads plans, approvals and results aloud (speechSynthesis); panel builds | L0 build, L3 judgement | 4 |
| B1 | brain/hands | recorded path runs without the model | `tests/test_fastbrowser_engine.py::test_recorded_path_replays_without_model_calls` | L0 | 4 |
| R1 | record/replay | every browser step records its path | `tests/test_fastbrowser_pipeline.py::test_fast_run_answers_from_the_final_page_and_records_its_path` | L0 | 4 |
| R2 | record/replay | automations replay with the fast engine | `tests/test_fastbrowser_pipeline.py::test_learned_once_replayed_fast` and `tests/test_automations.py` | L0 | 4 |
| R5 | record/replay | schedules run unattended | `tests/test_automations.py::test_a_due_schedule_starts_exactly_one_run`, `::test_runs_never_overlap` | L0 | 4 |
| A2 | any task | fixture benchmark, hands only | every task in `readiness/gates/test_a2_benchmark.py` passes with scripted decisions: search → open → extract, multi-field form with select and checkbox, pagination, saved login, back navigation, contenteditable editor | L0 | 5 |
| A3 | any task | frames | the fast engine reads and acts inside a same-origin iframe (`readiness/fixtures/frame.html`) | L0 | 5 |
| A4 | any task | uploads | a file input can be set from a path the operator approved (`readiness/fixtures/upload.html`) | L0 | 5 |
| A6 | any task | new tabs | a link that opens a new tab is followed and the result read (`readiness/fixtures/popup.html`) | L0 | 5 |
| C1 | cost | ledger | every task and step reports `cost_usd`, tokens in and out (`tests/test_costs.py`) | L0 | 5 |
| C3 | cost | budgets enforced | `tests/test_pipeline.py::test_budget_cap_stops_new_work`, `tests/test_limits.py` | L0 | 5 |
| C4 | cost | cost report | `GET /api/costs/summary` shows spend per role for a period and first-run vs replay spend per automation | L0 | 6 |
| C5 | cost | a simple task costs cents | live, through the whole of Fleet (planner, a browser step on a local profile, the answer): a one-step task on a public site finishes correctly for <= $0.03, and running it again as a saved automation costs <= $0.005; a task that names its page and one action never gets a planner round or a review round beyond what writes the answer | L1 + L2 | 2 |
| B3 | brain/hands | cheap decisions | three live Jev decisions on a fixture page: median cost <= $0.002 and median latency <= 4 s | L1 | 6 |
| B4 | brain/hands | cheap typed values | a live field-value call on the `fast` model costs <= $0.002 | L1 | 6 |
| R4 | record/replay | replay costs a tenth | the same fixture task run live twice (first with Jev, then from the recorded path): second run cost <= 10% of the first | L1 + browser | 6 |
| A7 | any task | live benchmark | with real Jev over the local Chrome: Wikipedia search → article → population, a public form, a two-page listing; >= 80% of the tasks finish with a correct answer | L1 + L2 | 7 |
| H1 | human | first real runs | FIRST_REAL_RUNS.md steps 1–6 done on the current build and noted | L3 | 8 |
| H2 | human | beyond the built-in assistant | one task the browser's built-in AI can't do (on the operator's own logged-in profile, or across several profiles, or on a schedule) done once and replayed three times from the automation, with the replay cost noted | L3 | 8 |
| H3 | human | voice session | ten minutes of browsing by voice judged usable: commands understood, readback useful, nothing clicked that wasn't asked for | L3 | 8 |
| H4 | human | the bill | total spend for one recorded task plus three replays measured and written down; replays together cost less than the first run | L3 | 8 |

## The exit rule

READY = every gate at L0, L1 and L2 passes **and** every H gate is `true` in `readiness/signoff.json`.

`python -m readiness` exits with:
- `0` READY;
- `2` every automated gate passes (or is unavailable on this machine) and only the operator's sign-off is missing;
- `1` at least one automated gate fails.

The loop runs while the exit code is `1`. It stops on `0` or `2` and says what is left.

## How gates are checked

- **pytest gates** name test files or test ids. `readiness` runs `tests/` once and `readiness/gates/` once (both on the
  throwaway Postgres, with a real headless browser) and reads the JUnit report, so one slow session serves every gate.
  A gate test for a feature that doesn't exist yet fails with the import error as its reason: that is the loop's to-do.
- **command gates** (F1, F3–F6) run the command and keep the last lines of its output as the reason.
- **live gates** (L1) spend real money. They stop at `FLEET_READINESS_BUDGET_USD` per run and append every call to
  `readiness/spend.jsonl`, so the bill is always visible. They never run without a key.
- **human gates** read `readiness/signoff.json`: `{"H1": {"done": true, "note": "...", "date": "2026-10-20"}}`.

## Adding or changing a gate

Edit `readiness/registry.py` (the registry: id, pillar, title, level, order, check) and this table in the same change;
`readiness/gates/test_s2_docs.py` fails when they drift apart. A gate is never deleted because it is hard; it is moved
down the order with a note in `AUTONOMOUS_PROGRESS.md` saying why.
