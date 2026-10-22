"""Check every readiness gate and write the scorecard (READINESS.md says what the gates mean).

    python -m readiness              # everything this machine can run
    python -m readiness --quick      # skip the full test suite, the panel build and the server boots
    python -m readiness --no-live    # never spend money (L1/L2 gates report unavailable)
    python -m readiness --gates A1,B2

Exit code: 0 READY, 2 only the operator's sign-off is missing, 1 at least one automated gate fails.
Writes readiness/scorecard.json and READINESS_STATUS.md.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path

from readiness import pg
from readiness.registry import Gate, ordered

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "readiness"
SCORECARD = OUT / "scorecard.json"
STATUS_MD = ROOT / "READINESS_STATUS.md"
BROWSERS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    "/usr/bin/google-chrome", "/usr/bin/chromium", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
]
PASS, FAIL, UNAVAILABLE, HUMAN = "pass", "fail", "unavailable", "waiting for the operator"


# -- environment --------------------------------------------------------------------------------


def load_env() -> None:
    """`.env` into the environment without overriding what is already set (same rule as Fleet itself)."""
    env_file = ROOT / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if value and key not in os.environ:
            os.environ[key] = value


def levels(no_live: bool) -> dict:
    keys = {k: bool(os.environ.get(k)) for k in ("OPENROUTER_API_KEY", "ANTHROPIC_API_KEY")}
    browser = next((b for b in BROWSERS if Path(b).exists()), None)
    return {
        "L0": pg.available() and browser is not None,
        "L1": any(keys.values()) and not no_live,
        "L2": any(keys.values()) and browser is not None and not no_live,
        "L3": True,
        "postgres": pg.available(),
        "browser": browser,
        "keys": keys,
        "node": shutil.which("npm") is not None,
    }


def python() -> str:
    return sys.executable


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def run(cmd: list[str], *, cwd: Path = ROOT, env: dict | None = None, timeout: int = 1800) -> tuple[int, str]:
    full = {**os.environ, **(env or {})}
    try:
        p = subprocess.run(cmd, cwd=str(cwd), env=full, capture_output=True, text=True, timeout=timeout,
                           encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired as e:
        return 124, f"timed out after {timeout}s\n{(e.stdout or '')[-2000:]}"
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def tail(text: str, n: int = 12) -> str:
    lines = [ln for ln in text.strip().splitlines() if ln.strip()]
    return "\n".join(lines[-n:])


# -- command gates ------------------------------------------------------------------------------


def check_import(ctx: dict) -> tuple[str, str]:
    code, out = run([python(), "-c", "import fleet, fleet.api, fleet.worker; print('ok')"], env={"FLEET_MASTER_KEY": ctx["master"]})
    if code != 0:
        return FAIL, tail(out)
    code, out = run([python(), "-c", "from fleet.cli import main; import sys; sys.argv=['fleet','--help']; main()"])
    return (PASS, "imports and `fleet --help` work") if code == 0 else (FAIL, tail(out))


def check_lint(ctx: dict) -> tuple[str, str]:
    code, out = run([python(), "-m", "ruff", "check", "src", "tests", "readiness", "scripts",
                     "--select", "E,F,W,B,UP,SIM", "--ignore", "E501,SIM105,SIM108,B008,UP017"])
    return (PASS, "ruff clean") if code == 0 else (FAIL, tail(out, 20))


def check_panel(ctx: dict) -> tuple[str, str]:
    if ctx["quick"]:
        index = ROOT / "web" / "dist" / "index.html"
        return (PASS, "web/dist exists (quick mode: not rebuilt)") if index.exists() else (FAIL, "web/dist/index.html missing")
    npm = shutil.which("npm")  # on Windows this is npm.cmd, which CreateProcess can't find by the bare name
    if not npm:
        return FAIL, "npm is not installed"
    if not (ROOT / "web" / "node_modules").exists():
        code, out = run([npm, "install", "--no-audit", "--no-fund"], cwd=ROOT / "web", timeout=900)
        if code != 0:
            return FAIL, "npm install failed: " + tail(out)
    code, out = run([npm, "run", "build"], cwd=ROOT / "web", timeout=900)
    if code != 0:
        return FAIL, tail(out, 20)
    return (PASS, "panel built") if (ROOT / "web" / "dist" / "index.html").exists() else (FAIL, "built, but no web/dist/index.html")


def _boot(env: dict, timeout: float = 30.0) -> tuple[str, str]:
    """Start `fleet serve --api-only` with `env`, wait for /api/health. The server's output goes to a file (unbuffered), never
    to a pipe nobody reads; the file is kept when FLEET_READINESS_KEEP_LOGS is set."""
    port = free_port()
    env = {**env, "FLEET_PORT": str(port), "FLEET_HOST": "127.0.0.1", "ANONYMIZED_TELEMETRY": "false"}
    log_path = OUT / f"boot-{port}.log"
    log_file = log_path.open("w", encoding="utf-8")
    proc = subprocess.Popen([python(), "-u", "-c", "from fleet.cli import main; import sys; sys.argv=['fleet','serve','--api-only']; main()"],
                            cwd=str(ROOT), env={**os.environ, **env}, stdout=log_file, stderr=subprocess.STDOUT)

    def output() -> str:
        log_file.flush()
        return log_path.read_text(encoding="utf-8", errors="replace")

    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                return FAIL, "server exited: " + tail(output())
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=15) as r:  # health probes AdsPower first (5 s)
                    if r.status == 200:
                        return PASS, f"/api/health answered 200 after {timeout - (deadline - time.monotonic()):.1f}s"
            except OSError:
                time.sleep(0.5)
        return FAIL, f"no answer from /api/health within {timeout:.0f}s; last output: " + tail(output(), 4)
    finally:
        _kill_tree(proc)
        log_file.close()
        if "FLEET_READINESS_KEEP_LOGS" not in os.environ:
            try:
                log_path.unlink(missing_ok=True)
            except OSError:
                pass


def _kill_tree(proc: subprocess.Popen) -> None:
    """The venv's python.exe is a launcher whose child is the real interpreter: killing only the launcher would leave the
    server (and its log file handle) running. taskkill /T takes the whole tree on Windows."""
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True)
    else:
        proc.kill()
    try:
        proc.wait(10)
    except subprocess.TimeoutExpired:
        pass


def check_server(ctx: dict) -> tuple[str, str]:
    if ctx["quick"]:
        return UNAVAILABLE, "quick mode: server boot skipped"
    if not ctx["dsn"]:
        return FAIL, "no throwaway Postgres (pip install pgserver)"
    return _boot({"FLEET_DATABASE_URL": pg.with_database(ctx["dsn"], "fleet_boot"), "FLEET_MASTER_KEY": ctx["master"],
                  "FLEET_DATA_DIR": str(OUT / ".data-boot")})


def _pg_ctl() -> str | None:
    try:
        import pgserver
    except ImportError:
        return None
    exe = Path(pgserver.__file__).parent / "pginstall" / "bin" / ("pg_ctl.exe" if sys.platform == "win32" else "pg_ctl")
    return str(exe) if exe.exists() else None


def _stop_embedded(data: Path) -> None:
    """Stop the Postgres a booted server started under `data` (pg_ctl daemonises it, so killing the server leaves it) and
    remove the directory, so the next check is a real first start again."""
    pgdata = data / "pg"
    if (pg_ctl := _pg_ctl()) and (pgdata / "postmaster.pid").exists():
        subprocess.run([pg_ctl, "stop", "-D", str(pgdata), "-m", "fast", "-w", "-t", "20"], capture_output=True)
    shutil.rmtree(data, ignore_errors=True)


def check_embedded(ctx: dict) -> tuple[str, str]:
    """A real first start: an empty data directory, initdb, the server up, /api/health answering; then stopped and removed."""
    if ctx["quick"]:
        return UNAVAILABLE, "quick mode: server boot skipped"
    data = OUT / ".data-embedded"
    _stop_embedded(data)
    if data.exists():
        return FAIL, f"{data} is still locked by an earlier embedded Postgres; stop that postgres.exe and run again"
    try:
        return _boot({"FLEET_DATABASE_URL": "embedded", "FLEET_MASTER_KEY": ctx["master"], "FLEET_DATA_DIR": str(data)}, timeout=240)
    finally:
        _stop_embedded(data)


CHECKS = {"check_import": check_import, "check_lint": check_lint, "check_panel": check_panel, "check_server": check_server,
          "check_embedded": check_embedded}


# -- pytest gates -------------------------------------------------------------------------------


def _node_ids(classname: str, name: str) -> str:
    """JUnit classname `tests.test_x.TestY` + name → `tests/test_x.py::TestY::name`."""
    parts = classname.split(".")
    for i in range(len(parts), 0, -1):
        candidate = ROOT.joinpath(*parts[:i]).with_suffix(".py")
        if candidate.exists():
            rest = parts[i:]
            return "::".join(["/".join(parts[:i]) + ".py", *rest, name])
    return f"{classname.replace('.', '/')}.py::{name}"


def read_junit(path: Path) -> dict[str, tuple[str, str]]:
    """node id → (status, reason) for every test case in a JUnit report."""
    results: dict[str, tuple[str, str]] = {}
    if not path.exists():
        return results
    for case in ET.parse(path).iter("testcase"):
        node = _node_ids(case.get("classname", ""), case.get("name", ""))
        status, reason = "passed", ""
        for child in case:
            if child.tag in ("failure", "error"):
                status, reason = "failed", (child.get("message") or child.text or "")[:400]
                break
            if child.tag == "skipped":
                status, reason = "skipped", (child.get("message") or "")[:200]
                break
        results[node] = (status, reason)
    return results


def run_pytest(targets: list[str], junit: Path, dsn: str | None, master: str, live: bool) -> str:
    """One pytest session over `targets`; the JUnit report is what the gates read. Returns a note when pytest itself broke."""
    env = {"FLEET_MASTER_KEY": master, "FLEET_READINESS_LIVE": "1" if live else "0", "PYTHONIOENCODING": "utf-8",
           # tests/conftest.py blanks the real key variables on import (tests must never spend by accident); the live gates
           # get the keys under their own names and put them back only for themselves (readiness/gates/conftest.py).
           "FLEET_READINESS_OPENROUTER_KEY": os.environ.get("OPENROUTER_API_KEY", ""),
           "FLEET_READINESS_ANTHROPIC_KEY": os.environ.get("ANTHROPIC_API_KEY", "")}
    if dsn:
        env["FLEET_TEST_DATABASE_URL"] = dsn
    cmd = [python(), "-m", "pytest", *targets, "-q", "-p", "no:cacheprovider", "--junit-xml", str(junit),
           "-o", "junit_family=xunit2", "-o", "addopts=", "--tb=line"]
    code, out = run(cmd, env=env, timeout=2400)
    return tail(out, 6) if code not in (0, 1, 5) else ""


def decide_pytest(gate: Gate, results: dict[str, tuple[str, str]]) -> tuple[str, str]:
    matched: dict[str, tuple[str, str]] = {}
    for target in gate.pytest:
        t = target.replace("\\", "/")
        for node, res in results.items():
            if node == t or node.startswith(t + "::") or node.startswith(t + "/") or node.startswith(t + "["):
                matched[node] = res
    if not matched:
        return FAIL, f"no test ran for {', '.join(gate.pytest)} (missing file, collection error, or not run in quick mode)"
    failed = {n: r for n, (s, r) in matched.items() if s == "failed"}
    if failed:
        node, reason = next(iter(failed.items()))
        return FAIL, f"{len(failed)}/{len(matched)} failed; first: {node.split('::')[-1]}: {reason}"
    skipped = {n: r for n, (s, r) in matched.items() if s == "skipped"}
    if len(skipped) == len(matched):
        return UNAVAILABLE, next(iter(skipped.values())) or "skipped"
    return PASS, f"{len(matched) - len(skipped)} test(s) passed" + (f", {len(skipped)} skipped" if skipped else "")


# -- human gates --------------------------------------------------------------------------------


def decide_human(gate: Gate) -> tuple[str, str]:
    try:
        signoff = json.loads((OUT / "signoff.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return HUMAN, f"readiness/signoff.json unreadable: {e}"
    entry = signoff.get(gate.id) or {}
    if entry.get("done") is True:
        return PASS, f"signed off {entry.get('date', '')}: {entry.get('note', '')}".strip()
    return HUMAN, "not signed off in readiness/signoff.json"


# -- the pass ------------------------------------------------------------------------------------


def evaluate(args) -> dict:
    load_env()
    lv = levels(args.no_live)
    master = "cmVhZGluZXNzLW1hc3Rlci1rZXktMzItYnl0ZXMtbG9uZy4u"  # 32 bytes, readiness runs only
    wanted = set(args.gates.split(",")) if args.gates else None
    gates = [g for g in ordered() if not wanted or g.id in wanted]

    dsn = None
    if lv["postgres"]:
        try:
            dsn = pg.start(OUT / ".pg")
        except Exception as e:  # noqa: BLE001 - the reason goes into the scorecard
            print(f"embedded Postgres failed to start: {e}", file=sys.stderr)
    ctx = {"levels": lv, "dsn": dsn, "master": master, "quick": args.quick}

    results: dict[str, tuple[str, str]] = {}
    notes: list[str] = []
    try:
        gate_targets = {t for g in gates for t in g.pytest}
        suite = [t for t in gate_targets if t == "tests" or t.startswith("tests/")]
        if suite:
            # The whole suite in one session (F2 needs it anyway); in quick mode only the files the chosen gates name.
            targets = ["tests"] if not args.quick else sorted({t.split("::")[0] for t in suite if t != "tests"})
            if targets:
                note = run_pytest(targets, OUT / "junit-tests.xml", dsn, master, lv["L1"])
                results.update(read_junit(OUT / "junit-tests.xml"))
                if note:
                    notes.append("tests/: " + note)
        if any(t.startswith("readiness/") for t in gate_targets):
            note = run_pytest(["readiness/gates"], OUT / "junit-gates.xml", dsn, master, lv["L1"])
            results.update(read_junit(OUT / "junit-gates.xml"))
            if note:
                notes.append("readiness/gates: " + note)

        rows = []
        for g in gates:
            started = time.monotonic()
            if g.human:
                status, reason = decide_human(g)
            elif g.cmd:
                status, reason = CHECKS[g.cmd](ctx)
            else:
                status, reason = decide_pytest(g, results)
            if status == FAIL and g.level in ("L1", "L2") and not lv[g.level]:
                status, reason = UNAVAILABLE, f"{g.level} not available here (no key or no browser)"
            rows.append({"id": g.id, "pillar": g.pillar, "title": g.title, "level": g.level, "order": g.order,
                         "status": status, "reason": reason, "passes_when": g.passes_when,
                         "seconds": round(time.monotonic() - started, 1)})
    finally:
        pg.stop()

    counts = {s: sum(r["status"] == s for r in rows) for s in (PASS, FAIL, UNAVAILABLE, HUMAN)}
    if counts[FAIL]:
        code = 1
    elif counts[HUMAN] or counts[UNAVAILABLE]:
        code = 2
    else:
        code = 0
    return {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "ready": code == 0,
        "exit_code": code,
        "quick": args.quick,
        "levels": {k: (v if not isinstance(v, str) else True) for k, v in lv.items() if k in ("L0", "L1", "L2", "L3")},
        "environment": {"browser": lv["browser"], "postgres": lv["postgres"], "node": lv["node"], "keys": lv["keys"]},
        "counts": counts,
        "notes": notes,
        "next": [r["id"] for r in sorted(rows, key=lambda r: (r["order"], r["id"])) if r["status"] == FAIL],
        "gates": rows,
    }


def merge(previous: dict, partial: dict) -> dict:
    """A run limited to some gates replaces only those rows in the last full scorecard, so the file stays complete."""
    rows = {r["id"]: r for r in previous.get("gates", [])}
    rows.update({r["id"]: r for r in partial["gates"]})
    order = {g.id: i for i, g in enumerate(ordered())}
    gates = sorted(rows.values(), key=lambda r: order.get(r["id"], 999))
    counts = {s: sum(r["status"] == s for r in gates) for s in (PASS, FAIL, UNAVAILABLE, HUMAN)}
    code = 1 if counts[FAIL] else (2 if counts[HUMAN] or counts[UNAVAILABLE] else 0)
    return {**previous, **partial, "gates": gates, "counts": counts, "exit_code": code, "ready": code == 0,
            "levels": previous.get("levels", partial["levels"]), "environment": previous.get("environment", partial["environment"]),
            "quick": previous.get("quick", partial["quick"]) and partial["quick"],
            "next": [r["id"] for r in gates if r["status"] == FAIL],
            "notes": previous.get("notes", []) + partial.get("notes", [])}


def write_status(card: dict) -> None:
    icon = {PASS: "✅", FAIL: "❌", UNAVAILABLE: "⏸", HUMAN: "🙋"}
    lines = [
        "# Readiness status",
        "",
        f"Generated {card['generated_at']} by `python -m readiness{' --quick' if card['quick'] else ''}`. "
        f"{'READY.' if card['ready'] else 'Not ready.'} "
        f"{card['counts'][PASS]} pass, {card['counts'][FAIL]} fail, {card['counts'][UNAVAILABLE]} unavailable here, "
        f"{card['counts'][HUMAN]} waiting for the operator.",
        "",
        "Levels on this machine: " + ", ".join(f"{k} {'yes' if v else 'no'}" for k, v in card["levels"].items())
        + f". Browser: {card['environment']['browser'] or 'none'}. Keys: "
        + ", ".join(f"{k} {'set' if v else 'missing'}" for k, v in card["environment"]["keys"].items()) + ".",
        "",
        "Next for the loop: " + (", ".join(card["next"]) or "nothing automated is failing") + ".",
        "",
        "| gate | pillar | status | why |",
        "|---|---|---|---|",
    ]
    for r in card["gates"]:
        why = r["reason"].replace("|", "\\|").replace("\n", " ")
        lines.append(f"| {r['id']} {r['title']} | {r['pillar']} ({r['level']}) | {icon[r['status']]} {r['status']} | {why[:220]} |")
    if card["notes"]:
        lines += ["", "Notes:", *[f"- {n}" for n in card["notes"]]]
    STATUS_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m readiness", description="Check the readiness gates (READINESS.md).")
    ap.add_argument("--quick", action="store_true", help="skip the full test suite, the panel build and the server boots")
    ap.add_argument("--no-live", action="store_true", help="never spend money: L1/L2 gates report unavailable")
    ap.add_argument("--gates", default="", help="comma-separated gate ids to check (default: all)")
    ap.add_argument("--json", action="store_true", help="print the scorecard as JSON")
    args = ap.parse_args(argv)
    card = evaluate(args)
    if args.gates and SCORECARD.exists():
        card = merge(json.loads(SCORECARD.read_text(encoding="utf-8")), card)
    OUT.mkdir(exist_ok=True)
    SCORECARD.write_text(json.dumps(card, indent=1, ensure_ascii=False), encoding="utf-8")
    write_status(card)
    if args.json:
        print(json.dumps(card, indent=1, ensure_ascii=False))
    else:
        for r in card["gates"]:
            print(f"{r['status']:<24} {r['id']:<3} {r['title']:<40} {r['reason'][:100]}")
        print(f"\n{'READY' if card['ready'] else 'not ready'}: {card['counts']}  next: {', '.join(card['next']) or '-'}")
        print(f"scorecard: {SCORECARD}\nstatus:    {STATUS_MD}")
    return card["exit_code"]


if __name__ == "__main__":
    raise SystemExit(main())
