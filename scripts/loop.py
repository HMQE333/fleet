"""Run Fleet's improvement loop until READINESS.md is satisfied (LOOP.md explains the rules).

    python scripts/loop.py [--max-iterations 10] [--budget-usd 15] [--hours 6] [--gate A1] [--dry-run]
                           [--permission-mode acceptEdits|bypassPermissions] [--model <id>] [--patience 3]
                           [--iteration-minutes 90] [--quick]

Each iteration: readiness → first failing gate → `claude -p` with the fleet-loop skill's procedure → readiness again.
Stops on READY (exit 0), when only the operator's sign-off is left (exit 2), or at a cap. Logs under readiness/loop/.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOOP_DIR = ROOT / "readiness" / "loop"
SCORECARD = ROOT / "readiness" / "scorecard.json"
SKILL = ROOT / ".claude" / "skills" / "fleet-loop" / "SKILL.md"
SPEND = ROOT / "readiness" / "spend.jsonl"


def python() -> str:
    for cand in (ROOT / ".venv" / "Scripts" / "python.exe", ROOT / ".venv" / "bin" / "python"):
        if cand.exists():
            return str(cand)
    return sys.executable


def readiness(quick: bool, no_live: bool) -> dict:
    cmd = [python(), "-m", "readiness"] + (["--quick"] if quick else []) + (["--no-live"] if no_live else [])
    print(f"[loop] {' '.join(cmd)}", flush=True)
    subprocess.run(cmd, cwd=str(ROOT), check=False)
    # The status file is tracked (it is the repo's own record of how far Fleet is), so the tree is clean again before
    # an iteration starts; the skill treats any other dirt as a reason to stop.
    subprocess.run(["git", "add", "READINESS_STATUS.md"], cwd=str(ROOT), capture_output=True)
    subprocess.run(["git", "commit", "-q", "-m", "readiness: status", "--", "READINESS_STATUS.md"], cwd=str(ROOT), capture_output=True)
    return json.loads(SCORECARD.read_text(encoding="utf-8"))


def spent_live() -> float:
    if not SPEND.exists():
        return 0.0
    return sum(json.loads(ln).get("cost_usd", 0.0) for ln in SPEND.read_text(encoding="utf-8").splitlines() if ln.strip())


def pick_gate(card: dict, forced: str | None, stalled: set[str]) -> dict | None:
    rows = {r["id"]: r for r in card["gates"]}
    if forced:
        return rows.get(forced)
    for gate_id in card["next"]:
        if gate_id not in stalled:
            return rows[gate_id]
    return None


def build_prompt(gate: dict, card: dict, iteration: int) -> str:
    skill = SKILL.read_text(encoding="utf-8").split("---", 2)[-1].strip()
    failing = ", ".join(card["next"]) or "-"
    return (
        f"{skill}\n\n"
        f"## This iteration (#{iteration}, {datetime.now(UTC).date()})\n\n"
        f"Gate: **{gate['id']} {gate['title']}** (pillar {gate['pillar']}, level {gate['level']}).\n"
        f"Passes when: {gate['passes_when'] or 'see READINESS.md'}.\n"
        f"Why it fails now: {gate['reason']}\n\n"
        f"Scorecard: {card['counts']}; failing in priority order: {failing}.\n"
        f"Do exactly one iteration for gate {gate['id']} following the procedure above, then stop."
    )


def preflight(args) -> None:
    """One tiny `claude -p` before any readiness pass: an expired login or a missing CLI must stop the loop now, not after
    twenty minutes of tests per iteration."""
    exe = shutil.which("claude")
    if not exe:
        raise SystemExit("claude (Claude Code CLI) is not on PATH; install it or run the loop from a terminal that has it")
    env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
    try:
        p = subprocess.run([exe, "-p", "Reply with the single word OK.", "--output-format", "json", "--max-turns", "1",
                            "--max-budget-usd", "0.5", *(["--model", args.model] if args.model else [])],
                           cwd=str(ROOT), env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
    except subprocess.TimeoutExpired as e:
        raise SystemExit("claude -p did not answer within two minutes; check the CLI by hand") from e
    try:
        data = json.loads(p.stdout)
    except ValueError:
        data = {}
    if p.returncode != 0 or data.get("is_error"):
        hint = "Log in first (run `claude auth login` in this terminal), then start the loop again."
        raise SystemExit("claude -p can't run: " + (data.get("result") or p.stderr.strip() or p.stdout.strip())[:300] + "  " + hint)
    print(f"[loop] claude -p works (${float(data.get('total_cost_usd') or 0):.4f} for the check)", flush=True)


def run_claude(prompt: str, args, log_path: Path) -> dict:
    exe = shutil.which("claude")
    if not exe:
        raise SystemExit("claude (Claude Code CLI) is not on PATH")
    cmd = [exe, "-p", prompt, "--output-format", "json", "--permission-mode", args.permission_mode,
           "--max-budget-usd", str(args.iteration_budget_usd), "--max-turns", str(args.max_turns)]
    if args.model:
        cmd += ["--model", args.model]
    env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}  # a nested session is refused otherwise
    started = time.monotonic()
    try:
        p = subprocess.run(cmd, cwd=str(ROOT), env=env, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=args.iteration_minutes * 60)
        out, err, code = p.stdout, p.stderr, p.returncode
    except subprocess.TimeoutExpired as e:
        out, err, code = (e.stdout or ""), (e.stderr or "") + f"\n[loop] timed out after {args.iteration_minutes} min", 124
    log_path.write_text(out + ("\n--- stderr ---\n" + err if err else ""), encoding="utf-8")
    result = {"exit": code, "minutes": round((time.monotonic() - started) / 60, 1), "cost_usd": 0.0, "summary": ""}
    try:
        data = json.loads(out)
        result["cost_usd"] = float(data.get("total_cost_usd") or 0.0)
        result["summary"] = (data.get("result") or "")[-1200:]
        result["turns"] = data.get("num_turns")
    except ValueError:
        result["summary"] = out[-1200:]
    return result


def main() -> int:
    ap = argparse.ArgumentParser(description="Fleet's improvement loop (LOOP.md).")
    ap.add_argument("--max-iterations", type=int, default=int(os.environ.get("FLEET_LOOP_MAX_ITERATIONS", "10")))
    ap.add_argument("--budget-usd", type=float, default=float(os.environ.get("FLEET_LOOP_BUDGET_USD", "15")))
    ap.add_argument("--hours", type=float, default=float(os.environ.get("FLEET_LOOP_HOURS", "6")))
    ap.add_argument("--gate", default=None, help="work on this gate only")
    ap.add_argument("--patience", type=int, default=3, help="iterations without progress before a gate is set aside")
    ap.add_argument("--permission-mode", default="acceptEdits", choices=["acceptEdits", "bypassPermissions", "default", "plan"])
    ap.add_argument("--model", default=os.environ.get("FLEET_LOOP_MODEL", "sonnet"),
                    help="model for the iterations (default sonnet: an Opus 1M iteration cost $27 in cache reads alone)")
    ap.add_argument("--iteration-budget-usd", type=float, default=float(os.environ.get("FLEET_LOOP_ITERATION_BUDGET_USD", "4")),
                    help="hard cap per iteration, enforced by claude -p --max-budget-usd")
    ap.add_argument("--max-turns", type=int, default=int(os.environ.get("FLEET_LOOP_MAX_TURNS", "200")),
                    help="hard cap on tool turns per iteration")
    ap.add_argument("--iteration-minutes", type=int, default=90)
    ap.add_argument("--quick", action="store_true", help="quick readiness between iterations (full at the end)")
    ap.add_argument("--no-live", action="store_true", help="never spend money in the readiness gates")
    ap.add_argument("--dry-run", action="store_true", help="show the gate and the prompt, call nothing")
    ap.add_argument("--reuse", action="store_true", help="start from the existing readiness/scorecard.json instead of a fresh run")
    args = ap.parse_args()

    LOOP_DIR.mkdir(parents=True, exist_ok=True)
    history = LOOP_DIR / "history.jsonl"
    if not args.dry_run:
        preflight(args)
    deadline = time.monotonic() + args.hours * 3600
    spent = 0.0
    live_before = spent_live()
    stalled: set[str] = set()
    attempts: dict[str, int] = {}
    if args.reuse and SCORECARD.exists():
        card = json.loads(SCORECARD.read_text(encoding="utf-8"))
        print(f"[loop] reusing scorecard from {card['generated_at']}", flush=True)
    else:
        card = readiness(args.quick, args.no_live)

    for i in range(1, args.max_iterations + 1):
        if card["exit_code"] in (0, 2):
            break
        if time.monotonic() > deadline:
            print("[loop] out of time", flush=True)
            break
        if spent + (spent_live() - live_before) > args.budget_usd:
            print(f"[loop] budget reached (${spent:.2f} + live gates)", flush=True)
            break
        gate = pick_gate(card, args.gate, stalled)
        if gate is None:
            print("[loop] every failing gate is stalled; stopping", flush=True)
            break
        before = card["counts"]["pass"]
        prompt = build_prompt(gate, card, i)
        print(f"\n[loop] iteration {i}: gate {gate['id']} {gate['title']} ({gate['reason'][:120]})", flush=True)
        if args.dry_run:
            print(prompt[-900:])
            return 0
        result = run_claude(prompt, args, LOOP_DIR / f"iter-{i:03d}.log")
        spent += result["cost_usd"]
        card = readiness(args.quick, args.no_live)
        after = card["counts"]["pass"]
        gate_now = next((r["status"] for r in card["gates"] if r["id"] == gate["id"]), "?")
        progressed = after > before or gate_now == "pass"
        attempts[gate["id"]] = 0 if progressed else attempts.get(gate["id"], 0) + 1
        if attempts[gate["id"]] >= args.patience:
            stalled.add(gate["id"])
            print(f"[loop] {gate['id']} set aside after {args.patience} iterations without progress", flush=True)
        entry = {"at": datetime.now(UTC).isoformat(timespec="seconds"), "iteration": i, "gate": gate["id"], "gate_status": gate_now,
                 "pass_before": before, "pass_after": after, "claude_cost_usd": round(result["cost_usd"], 4),
                 "minutes": result["minutes"], "exit": result["exit"], "summary": result["summary"][-400:]}
        with history.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        print(f"[loop] {gate['id']} → {gate_now}; pass {before} → {after}; ${result['cost_usd']:.2f}, {result['minutes']} min", flush=True)

    if args.quick and not args.dry_run:
        card = readiness(False, args.no_live)
    print(f"\n[loop] done: {'READY' if card['ready'] else 'not ready'} {card['counts']}; Claude spend ${spent:.2f}, "
          f"live gates ${spent_live() - live_before:.2f}; next: {', '.join(card['next']) or '-'}")
    return card["exit_code"]


if __name__ == "__main__":
    raise SystemExit(main())
