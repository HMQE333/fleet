"""The documentation must not drift from the code: every setting it names exists, every file it points to exists."""

import pathlib
import re

import pytest

from fleet.config import Settings

pytestmark = pytest.mark.no_db

ROOT = pathlib.Path(__file__).resolve().parent.parent
DOCS = ["README.md", "RULES.md", ".env.example", "FIRST_REAL_RUNS.md", "ROADMAP_AUTOMATION.md"]
READ_FROM_THE_ENVIRONMENT_DIRECTLY = {"FLEET_MASTER_KEY",  # the vault reads it itself
                                      "FLEET_TEST_DATABASE_URL"}  # only the tests read it (tests/conftest.py)


def _text(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def test_every_FLEET_variable_the_docs_name_is_a_real_setting():
    known = {f"FLEET_{name.upper()}" for name in Settings.model_fields} | READ_FROM_THE_ENVIRONMENT_DIRECTLY
    problems = []
    for doc in DOCS:
        for var in sorted(set(re.findall(r"\bFLEET_[A-Z][A-Z0-9_]*\b", _text(doc)))):
            if var not in known and not var.endswith("_"):  # "FLEET_" prefixes written in prose end with an underscore
                problems.append(f"{doc}: {var}")
    assert problems == [], "documented settings that don't exist: " + ", ".join(problems)


def test_every_source_or_test_file_the_rules_and_readme_point_to_exists():
    problems = []
    for doc in ("README.md", "RULES.md", "FIRST_REAL_RUNS.md"):
        for ref in sorted(set(re.findall(r"`((?:fleet|tests)/[\w/]+\.(?:py|sql))`", _text(doc)))):
            candidates = [ROOT / "src" / ref, ROOT / ref]
            if not any(c.exists() for c in candidates):
                problems.append(f"{doc}: {ref}")
    assert problems == [], "documented files that don't exist: " + ", ".join(problems)


def test_every_documented_setting_that_can_be_changed_is_in_the_example_env_file():
    """.env.example is where an operator looks first: each limit documented as an environment variable is listed there."""
    example = _text(".env.example")
    for var in ("FLEET_SCRAPE_MAX_PAGES", "FLEET_SCRAPE_MAX_ITEMS", "FLEET_SCRAPE_MAX_DETAILS", "FLEET_CRAWL_MAX_PAGES",
                "FLEET_CRAWL_MAX_DEPTH", "FLEET_REGISTRATIONS_PER_DAY", "FLEET_RETENTION_DAYS"):
        assert var in example, var


def test_the_documentation_set_the_readme_promises_exists():
    for name in ("RULES.md", "ROADMAP_AUTOMATION.md", "FIRST_REAL_RUNS.md", "FUTURE_IDEAS.md", "AUTONOMOUS_PROGRESS.md"):
        assert (ROOT / name).exists(), name
