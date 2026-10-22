"""The readiness gates: what must be true for Fleet to be called ready (READINESS.md has the words).

Each gate is decided by tests (`pytest` targets: files or node ids, run once per readiness pass and read from the
JUnit report), by a command (`cmd`: a function in readiness/run.py) or by the operator (`human`: readiness/signoff.json).
Keep this list and the table in READINESS.md in step: readiness/gates/test_s2_docs.py fails when they drift.
"""

from dataclasses import dataclass, field

SAFETY_SUITES = [
    "tests/test_api_security.py", "tests/test_api_sweep.py", "tests/test_trust.py", "tests/test_vault.py",
    "tests/test_registration.py", "tests/test_never_register.py", "tests/test_site_logins.py", "tests/test_login_steps.py",
    "tests/test_webfetch.py", "tests/test_fastbrowser_unit.py",
]


@dataclass(frozen=True)
class Gate:
    id: str
    pillar: str
    title: str
    level: str  # L0 | L1 | L2 | L3
    order: int  # the loop works on lower numbers first
    pytest: tuple[str, ...] = ()  # test files or node ids that decide it
    cmd: str | None = None  # name of a check_* function in readiness/run.py
    human: bool = False  # decided by readiness/signoff.json
    passes_when: str = ""
    tags: tuple[str, ...] = field(default_factory=tuple)


GATES: list[Gate] = [
    # -- rules: never traded away ------------------------------------------------------------------
    Gate("S1", "rules", "safety suites green", "L0", 0, pytest=tuple(SAFETY_SUITES),
         passes_when="every safety test file passes"),
    Gate("S2", "rules", "docs match code", "L0", 0, pytest=("tests/test_docs.py", "readiness/gates/test_s2_docs.py"),
         passes_when="documented settings and files exist; READINESS.md and gates.py agree"),
    # -- foundation ------------------------------------------------------------------------------
    Gate("F1", "foundation", "installs and imports", "L0", 1, cmd="check_import",
         passes_when="import fleet, fleet.api, fleet.worker works; `fleet --help` exits 0"),
    Gate("F2", "foundation", "tests green", "L0", 1, pytest=("tests",),
         passes_when="the whole tests/ suite passes on a throwaway Postgres"),
    Gate("F3", "foundation", "lint clean", "L0", 1, cmd="check_lint", passes_when="ruff reports nothing"),
    Gate("F4", "foundation", "panel builds", "L0", 1, cmd="check_panel",
         passes_when="npm run build succeeds and web/dist/index.html exists"),
    Gate("F5", "foundation", "server boots", "L0", 1, cmd="check_server",
         passes_when="fleet serve --api-only answers /api/health within 30 s on a throwaway database"),
    Gate("F6", "foundation", "runs without external Postgres", "L0", 1, cmd="check_embedded",
         passes_when="FLEET_DATABASE_URL=embedded starts an embedded Postgres under data/pg and the server boots on it"),
    # -- the enablers: order 2 ---------------------------------------------------------------------
    Gate("A1", "any task", "local Chrome, no AdsPower", "L0", 2, pytest=("readiness/gates/test_a1_local_browser.py",),
         passes_when="a profile of kind local opens its own Chrome and both browser workers can drive it"),
    Gate("B2", "brain/hands", "the brain writes a macro", "L0", 2, pytest=("readiness/gates/test_b2_macro.py",),
         passes_when="fleet.macro validates a step list and runs it in the fast engine with zero model calls"),
    Gate("R3", "record/replay", "self-healing replay", "L0", 2, pytest=("readiness/gates/test_r3_self_healing.py",),
         passes_when="one changed label costs one decision and the saved path is updated"),
    Gate("C5", "cost", "a simple task costs cents", "L2", 2, pytest=("readiness/gates/test_live.py::test_c5_a_simple_task_costs_cents",),
         passes_when="a one-step task on a public site, through planner + browser + answer, <= $0.03; its saved replay <= $0.005"),
    # -- the experience base: what every action taught Fleet -----------------------------------------
    Gate("K1", "experience", "every step is written down", "L0", 2, pytest=("readiness/gates/test_k1_experience_recorded.py",),
         passes_when="every browser step writes domain, goal, actions, outcome, reason, cost, seconds into `experience`"),
    Gate("K2", "experience", "the base answers questions", "L0", 3, pytest=("readiness/gates/test_k2_experience_queries.py",),
         passes_when="best_path(domain, goal) and site_notes(domain) answer from recorded experience"),
    Gate("K3", "experience", "brain and hands use it", "L0", 3, pytest=("readiness/gates/test_k3_experience_used.py",),
         passes_when="a step without a saved path gets best_path as its guide; the planner prompt carries site_notes"),
    Gate("K4", "experience", "the operator can read and prune it", "L0", 5, pytest=("readiness/gates/test_k4_experience_api.py",),
         passes_when="GET/DELETE /api/experience and a Knowledge page in the panel"),
    Gate("K5", "experience", "the second visit is cheaper", "L1", 6, pytest=("readiness/gates/test_live.py::test_k5_second_visit_is_cheaper",),
         passes_when="a paraphrased task on a known site costs <= 30% of the first run"),
    # -- voice and the Claude API: order 3 ---------------------------------------------------------
    Gate("V1", "voice", "voice grammar", "L0", 3, pytest=("readiness/gates/test_v1_voice_grammar.py",),
         passes_when="fleet.voice.parse >= 95% on readiness/benchmarks/voice_utterances.json"),
    Gate("V2", "voice", "voice endpoints", "L0", 3, pytest=("readiness/gates/test_v2_voice_api.py",),
         passes_when="POST /api/voice/command and /api/voice/task exist, work and are guarded"),
    Gate("V4", "voice", "voice latency", "L0", 3, pytest=("readiness/gates/test_v4_voice_latency.py",),
         passes_when="a navigation command executes within 1 s of being parsed"),
    Gate("C2", "cost", "Claude API as a brain provider", "L0", 3, pytest=("readiness/gates/test_c2_anthropic.py",),
         passes_when="brain roles can run on the Anthropic API with cost accounting; cheap roles stay on OpenRouter"),
    # -- order 4: what already works must keep working --------------------------------------------
    Gate("V3", "voice", "push-to-talk in the panel", "L0", 4, pytest=("readiness/gates/test_v3_panel_voice.py",),
         passes_when="the panel has a microphone control and spoken readback; panel builds"),
    Gate("B1", "brain/hands", "recorded path runs without the model", "L0", 4,
         pytest=("tests/test_fastbrowser_engine.py::test_recorded_path_replays_without_model_calls",)),
    Gate("R1", "record/replay", "every browser step records its path", "L0", 4,
         pytest=("tests/test_fastbrowser_pipeline.py::test_fast_run_answers_from_the_final_page_and_records_its_path",)),
    Gate("R2", "record/replay", "automations replay with the fast engine", "L0", 4,
         pytest=("tests/test_fastbrowser_pipeline.py::test_learned_once_replayed_fast", "tests/test_automations.py")),
    Gate("R5", "record/replay", "schedules run unattended", "L0", 4,
         pytest=("tests/test_automations.py::test_a_due_schedule_starts_exactly_one_run",
                 "tests/test_automations.py::test_runs_never_overlap")),
    # -- order 5: fewer limits ----------------------------------------------------------------------
    Gate("A2", "any task", "fixture benchmark, hands only", "L0", 5, pytest=("readiness/gates/test_a2_benchmark.py",),
         passes_when="search/open/extract, a multi-field form, pagination, a saved login, back navigation, an editor"),
    Gate("A3", "any task", "frames", "L0", 5, pytest=("readiness/gates/test_a3_frames.py",),
         passes_when="the fast engine reads and acts inside a same-origin iframe"),
    Gate("A4", "any task", "uploads", "L0", 5, pytest=("readiness/gates/test_a4_upload.py",),
         passes_when="a file input can be set from an approved path"),
    Gate("A6", "any task", "new tabs", "L0", 5, pytest=("readiness/gates/test_a6_new_tab.py",),
         passes_when="a link that opens a new tab is followed and read"),
    Gate("C1", "cost", "ledger", "L0", 5, pytest=("tests/test_costs.py",)),
    Gate("C3", "cost", "budgets enforced", "L0", 5,
         pytest=("tests/test_pipeline.py::test_budget_cap_stops_new_work", "tests/test_limits.py")),
    # -- order 6/7: measured with real money ---------------------------------------------------------
    Gate("C4", "cost", "cost report", "L0", 6, pytest=("readiness/gates/test_c4_cost_report.py",),
         passes_when="GET /api/costs/summary: spend per role, first run vs replay per automation"),
    Gate("B3", "brain/hands", "cheap decisions", "L1", 6, pytest=("readiness/gates/test_live.py::test_b3_jev_decision_is_cheap_and_fast",),
         passes_when="median Jev decision <= $0.002 and <= 4 s"),
    Gate("B4", "brain/hands", "cheap typed values", "L1", 6, pytest=("readiness/gates/test_live.py::test_b4_field_value_is_cheap",),
         passes_when="one field-value call on the fast model <= $0.002"),
    Gate("R4", "record/replay", "replay costs a tenth", "L1", 6, pytest=("readiness/gates/test_live.py::test_r4_replay_costs_a_tenth",),
         passes_when="second (recorded) run <= 10% of the first"),
    Gate("A7", "any task", "live benchmark", "L2", 7, pytest=("readiness/gates/test_live.py::test_a7_live_benchmark",),
         passes_when=">= 80% of the live tasks finish with a correct answer"),
    # -- the operator's judgement --------------------------------------------------------------------
    Gate("H1", "human", "first real runs", "L3", 8, human=True, passes_when="FIRST_REAL_RUNS.md steps 1-6 done on this build"),
    Gate("H2", "human", "beyond the built-in assistant", "L3", 8, human=True,
         passes_when="a task the built-in AI can't do, done once and replayed three times"),
    Gate("H3", "human", "voice session", "L3", 8, human=True, passes_when="ten minutes of voice browsing judged usable"),
    Gate("H4", "human", "the bill", "L3", 8, human=True, passes_when="one task + three replays measured; replays cost less than the first run"),
]

BY_ID = {g.id: g for g in GATES}


def ordered() -> list[Gate]:
    return sorted(GATES, key=lambda g: (g.order, g.id))
