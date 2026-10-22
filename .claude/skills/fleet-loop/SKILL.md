---
name: fleet-loop
description: One iteration of Fleet's improvement loop - run the readiness gates, take the first failing one, make one verified change, commit, log. TRIGGER on "/fleet-loop", "pętla fleet", "ulepsz fleet", "następna bramka", "next gate", "popraw gotowość".
---

# Fleet improvement loop: one iteration

You are improving Fleet, an agentic browser (README.md), towards the definition of ready in READINESS.md. One iteration
is one gate, one verified change, one commit. Work in the project root (the folder with pyproject.toml). Use the project
venv: `.venv\Scripts\python` on Windows, `.venv/bin/python` elsewhere (bootstrap with `scripts/bootstrap.ps1` if missing).

## 1. Where things stand (5 minutes, no changes yet)

1. Read `readiness/scorecard.json`. If it is older than 2 hours or missing, run `python -m readiness --quick --no-live`
   first (full: `python -m readiness`; it takes several minutes and may spend up to FLEET_READINESS_BUDGET_USD).
2. Take the gate: the one you were given (`$ARGUMENTS` or the prompt), else the first id in `scorecard["next"]`
   (failing gates in priority order). Never an S gate unless it is the one failing. Read its row in READINESS.md and its
   test file in `readiness/gates/` or `tests/`: the test's docstring describes the expected shape; you may build another
   shape if every assert still holds and the docstring is updated with you.
3. Read the tail of `AUTONOMOUS_PROGRESS.md` (the last two entries) and RULES.md once per session.
4. Check `git status`: start from a clean tree. `READINESS_STATUS.md` and `readiness/scorecard.json` are the loop's own output
   and may be modified: commit `READINESS_STATUS.md` alone as "readiness: status" and go on. Anything else dirty: stop and say what is there.

## 2. One verified change

1. Decide the smallest change that makes the gate pass without weakening any other gate. Write it down in one paragraph
   before you code (it becomes the progress-log entry).
2. Build it. Conventions: plain SQL migrations (never edit an applied one; add `NNN_name.sql`), prompts in
   `fleet/prompts.py`, every model answer validated with pydantic, code decides permissions (RULES.md "Enforced by"),
   docs in the same commit as the behaviour. Write patch scripts with the Write tool, not shell heredocs.
3. Verify, in this order, all with the venv python:
   - the gate's own test(s): `python -m pytest readiness/gates/<file> -q` (or the `tests/...` id). Needs
     `FLEET_TEST_DATABASE_URL`: run `python scripts/pgtest.py` in another terminal and export the URI it prints;
   - the full suite: `python -m pytest -q` (with the same env; must stay green);
   - lint: `python -m ruff check src tests readiness scripts --select E,F,W,B,UP,SIM --ignore E501,SIM105,SIM108,B008,UP017`;
   - `cd web && npm run build` whenever `web/` changed;
   - a mutation check for the new behaviour: break it on purpose, see the test fail, restore it.
4. If the gate still fails after a genuine attempt, do not fake it and do not loosen the test: leave the tree clean
   (`git checkout -- . && git clean -fd` for your own files), write what you learned to the progress log, and end the
   iteration honestly as "no progress on <gate>".

## 3. Judge, keep or revert

Keep the change only if all of these hold: the gate's tests pass, `tests/` is green, ruff is clean, no S gate went red,
RULES.md still describes the code. Otherwise revert everything. Never commit a half-state.

## 4. Commit and log

1. Append to `AUTONOMOUS_PROGRESS.md` under a heading `## Loop run <next number> - <gate id>: <gate title> (<date>)`:
   what was built (files), how it was verified (test names, mutation check), what was measured (cost, latency, counts),
   what is left or deliberately not done.
2. Update READINESS.md only if the gate's wording had to change, and `readiness/registry.py` in the same commit.
3. `git add -A` (never `.env`, never `readiness/.pg`, `readiness/.data-*`, `readiness/loop/`, `readiness/junit-*.xml`,
   `readiness/spend.jsonl`), then one commit: `<gate id>: <what it does now>` with a body of three to six lines. End the
   message with the attribution line the session gave you, if any. Never push.
4. Finish with a five-line summary: gate, result (pass / no progress), what changed, what you measured, what the next gate is.
   (Do not run the readiness runner yourself: the loop runs it after you, and it takes twenty minutes you would pay to read.)

## Keep the transcript small (this is where the money goes)

Every tool result you read is re-read on every later turn. One iteration on a 1M-context model cost $27 almost entirely in
cache reads, so:
- never print a whole test run: `python -m pytest <target> -q -x --tb=line 2>&1 | tail -15`; the full suite ONCE, at the end,
  the same way; never `-v`, never the readiness runner's full output (the loop runs readiness before and after you; you don't);
- read files by range (`sed -n 120,180p`) or search (`grep -n`), never whole files over ~150 lines, never READINESS_STATUS.md or
  scorecard.json (the prompt already holds your gate and why it fails);
- no exploratory reading of the panel, the web build or node_modules unless the gate is about the panel;
- never run anything in the background (no `run_in_background`, no `&`): a print-mode session is not woken when a background
  job ends, so it would stop with the work uncommitted. Run tests in the foreground with a timeout and wait;
- one mutation check, run on the gate's test file only;
- when the full suite is green and ruff is clean, commit and stop: no second look, no extra polish, no summary longer than five lines.

## Never

- never type, log or commit a secret; never read `.env` into output;
- never solve or bypass captchas, never add stealth, proxies, bulk accounts, fake engagement (RULES.md);
- never run the live gates without a key or over budget; never restart a server the operator is using;
- never do two gates in one iteration, never leave the tree dirty, never force-push or rewrite history.
