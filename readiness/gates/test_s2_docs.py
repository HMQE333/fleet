"""READINESS.md and readiness/registry.py must name the same gates, and every test a gate points to must exist."""

import pathlib
import re

import pytest

from readiness.registry import GATES

pytestmark = pytest.mark.no_db

ROOT = pathlib.Path(__file__).resolve().parents[2]


def test_the_readiness_table_and_the_registry_agree():
    text = (ROOT / "READINESS.md").read_text(encoding="utf-8")
    table_ids = set(re.findall(r"^\| ([SFABRVCHK]\d) \|", text, re.M))  # gate rows, not the L0-L3 levels table
    registry_ids = {g.id for g in GATES}
    assert table_ids == registry_ids, f"only in READINESS.md: {table_ids - registry_ids}; only in registry.py: {registry_ids - table_ids}"


def test_every_gate_names_a_test_file_or_check_that_exists():
    from readiness import run

    problems = []
    for g in GATES:
        if g.cmd and g.cmd not in run.CHECKS:
            problems.append(f"{g.id}: no {g.cmd} in readiness/run.py")
        for target in g.pytest:
            path = ROOT / target.split("::")[0]
            if not path.exists():
                problems.append(f"{g.id}: {target} does not exist")
    assert problems == [], problems


def test_the_loop_skill_and_loop_docs_exist():
    for name in ("LOOP.md", ".claude/skills/fleet-loop/SKILL.md", "scripts/loop.py", "readiness/signoff.json"):
        assert (ROOT / name).exists(), name
