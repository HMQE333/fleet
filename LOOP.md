# The improvement loop

Fleet improves itself in a loop that runs until READINESS.md is satisfied: check the gates, pick the first failing one,
make one verified change, prove it, commit, check again. Two ways to run it; both follow the same procedure, which lives
in `.claude/skills/fleet-loop/SKILL.md` so the Claude session and the script never disagree about the rules.

## Setup once

```powershell
# in the project folder (keep it short, e.g. C:\fleet - Windows paths over 260 characters break Python and git)
.\scripts\bootstrap.ps1        # venv, dependencies, embedded Postgres, panel build
.\scripts\set_key.ps1         # asks for ANTHROPIC_API_KEY (hidden input) and writes it to .env; same for OPENROUTER_API_KEY
                               # unless it is already in your Windows environment. If PowerShell refuses to run scripts:
                               # powershell -ExecutionPolicy Bypass -File .\scripts\set_key.ps1
.venv\Scripts\python -m readiness --quick   # first scorecard: readiness\scorecard.json, READINESS_STATUS.md
```

`OPENROUTER_API_KEY` already set in your Windows user environment is picked up; `.env` never overrides a variable that is
already set. Keys in `.env` stay on this machine (`.env` is git-ignored) and are never printed by the loop.

## Run the loop unattended (terminal)

```powershell
.venv\Scripts\python scripts\loop.py --max-iterations 10 --budget-usd 15 --hours 6
```

Each iteration:
1. `python -m readiness` → `readiness/scorecard.json`. Exit 0 or 2 ends the loop (ready, or only your sign-off is left).
2. The first failing gate in `order` becomes the iteration's goal (`--gate X` forces one; a gate that made no progress
   in three iterations is set aside and the next one is tried).
3. `claude -p` runs with the skill's procedure, the gate, and the scorecard. The run is logged to
   `readiness/loop/iter-NNN.log`; its own cost (from `--output-format json`) and the live gates' spend
   (`readiness/spend.jsonl`) count against `--budget-usd`.
4. Readiness runs again. Pass counts before and after go to `readiness/loop/history.jsonl`.

Stops: READY, only sign-off left, `--max-iterations`, `--hours`, `--budget-usd`, or three iterations in a row with no
gate improved (`--patience`). `--dry-run` prints what it would do without calling Claude. `--permission-mode` is passed
through to Claude Code (`acceptEdits` by default; `bypassPermissions` for a machine nobody is watching, your call).

## Run it inside this app

Open the project folder in Claude Code (desktop) and type `/fleet-loop` for one iteration, or `/loop /fleet-loop` to
repeat it until it stops itself. Same procedure, same logs; you watch and can interrupt.

## What an iteration may and may not do

- One gate per iteration, one verified change, one commit on `master`. Never two gates at once, never an unverified commit.
- RULES.md and the S gates are never traded for progress. A change that turns any S gate red is reverted, not patched over.
- Tests before and after: the loop keeps `tests/` green (F2) and `ruff` clean (F3); the panel builds (F4) when `web/` was touched.
- Real money only in the live gates, within `FLEET_READINESS_BUDGET_USD` per readiness run; the iteration's own model use is
  Claude Code's, capped by `--budget-usd` for the whole loop.
- `.env` is never read into a log, never committed, never printed.
- Every iteration appends a dated entry to `AUTONOMOUS_PROGRESS.md`: gate, what was built, how it was verified, what was
  measured, what it cost, what is left.
- A gate test may be loosened only when it is wrong (then the fix is the test, with a sentence in the progress log saying
  why); never because the feature is hard.

## The operator's part (L3)

When the automated gates are green the loop stops and asks for `readiness/signoff.json`: FIRST_REAL_RUNS 1–6 on the
current build (H1), one task the browser's built-in AI can't do, replayed three times (H2), ten minutes of voice browsing
(H3), and the bill for a task plus its replays (H4). Set each `done` to `true` with a note and a date. Then
`python -m readiness` exits 0: Fleet is ready.
