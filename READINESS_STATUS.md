# Readiness status

Generated 2026-10-19T18:33:37+00:00 by `python -m readiness`. Not ready. 36 pass, 1 fail, 0 unavailable here, 4 waiting for the operator.

Levels on this machine: L0 yes, L1 yes, L2 yes, L3 yes. Browser: C:\Program Files\Google\Chrome\Application\chrome.exe. Keys: OPENROUTER_API_KEY set, ANTHROPIC_API_KEY set.

Next for the loop: B3.

| gate | pillar | status | why |
|---|---|---|---|
| S1 safety suites green | rules (L0) | ✅ pass | 218 test(s) passed |
| S2 docs match code | rules (L0) | ✅ pass | 7 test(s) passed |
| F1 installs and imports | foundation (L0) | ✅ pass | imports and `fleet --help` work |
| F2 tests green | foundation (L0) | ✅ pass | 663 test(s) passed |
| F3 lint clean | foundation (L0) | ✅ pass | ruff clean |
| F4 panel builds | foundation (L0) | ✅ pass | panel built |
| F5 server boots | foundation (L0) | ✅ pass | /api/health answered 200 after 4.6s |
| F6 runs without external Postgres | foundation (L0) | ✅ pass | /api/health answered 200 after 15.2s |
| A1 local Chrome, no AdsPower | any task (L0) | ✅ pass | 3 test(s) passed |
| B2 the brain writes a macro | brain/hands (L0) | ✅ pass | 3 test(s) passed |
| C5 a simple task costs cents | cost (L2) | ✅ pass | 1 test(s) passed |
| K1 every step is written down | experience (L0) | ✅ pass | 2 test(s) passed |
| R3 self-healing replay | record/replay (L0) | ✅ pass | 2 test(s) passed |
| C2 Claude API as a brain provider | cost (L0) | ✅ pass | 3 test(s) passed |
| K2 the base answers questions | experience (L0) | ✅ pass | 2 test(s) passed |
| K3 brain and hands use it | experience (L0) | ✅ pass | 2 test(s) passed |
| V1 voice grammar | voice (L0) | ✅ pass | 2 test(s) passed |
| V2 voice endpoints | voice (L0) | ✅ pass | 3 test(s) passed |
| V4 voice latency | voice (L0) | ✅ pass | 1 test(s) passed |
| B1 recorded path runs without the model | brain/hands (L0) | ✅ pass | 1 test(s) passed |
| R1 every browser step records its path | record/replay (L0) | ✅ pass | 1 test(s) passed |
| R2 automations replay with the fast engine | record/replay (L0) | ✅ pass | 15 test(s) passed |
| R5 schedules run unattended | record/replay (L0) | ✅ pass | 2 test(s) passed |
| V3 push-to-talk in the panel | voice (L0) | ✅ pass | 2 test(s) passed |
| A2 fixture benchmark, hands only | any task (L0) | ✅ pass | 6 test(s) passed |
| A3 frames | any task (L0) | ✅ pass | 1 test(s) passed |
| A4 uploads | any task (L0) | ✅ pass | 1 test(s) passed |
| A6 new tabs | any task (L0) | ✅ pass | 1 test(s) passed |
| C1 ledger | cost (L0) | ✅ pass | 5 test(s) passed |
| C3 budgets enforced | cost (L0) | ✅ pass | 19 test(s) passed |
| K4 the operator can read and prune it | experience (L0) | ✅ pass | 2 test(s) passed |
| B3 cheap decisions | brain/hands (L1) | ❌ fail | 1/1 failed; first: test_b3_jev_decision_is_cheap_and_fast: AssertionError: [10.289216999997734, 9.455471499997657, 5.683522400009679] assert 9.455471499997657 <= 4.0  +  where 9.455471499997657 = <function median at 0x00 |
| B4 cheap typed values | brain/hands (L1) | ✅ pass | 1 test(s) passed |
| C4 cost report | cost (L0) | ✅ pass | 1 test(s) passed |
| K5 the second visit is cheaper | experience (L1) | ✅ pass | 1 test(s) passed |
| R4 replay costs a tenth | record/replay (L1) | ✅ pass | 1 test(s) passed |
| A7 live benchmark | any task (L2) | ✅ pass | 1 test(s) passed |
| H1 first real runs | human (L3) | 🙋 waiting for the operator | not signed off in readiness/signoff.json |
| H2 beyond the built-in assistant | human (L3) | 🙋 waiting for the operator | not signed off in readiness/signoff.json |
| H3 voice session | human (L3) | 🙋 waiting for the operator | not signed off in readiness/signoff.json |
| H4 the bill | human (L3) | 🙋 waiting for the operator | not signed off in readiness/signoff.json |
