"""V1: spoken words become commands, in Polish and English, without a model.

Expected shape: `fleet.voice.parse(text: str) -> Command | None` where Command has `.kind` and `.args`:
  kinds: open(url) | search(query) | click(label) | type(text) | scroll(direction) | back | forward | tab(n|next|prev)
         | read | stop | approve | reject | task(prompt) | help
The utterances and expected commands live in readiness/benchmarks/voice_utterances.json.
"""

import importlib
import json
import pathlib

import pytest

pytestmark = pytest.mark.no_db

BENCH = pathlib.Path(__file__).resolve().parents[1] / "benchmarks" / "voice_utterances.json"


def test_the_grammar_understands_at_least_95_percent():
    try:
        voice = importlib.import_module("fleet.voice")
    except ImportError as e:
        pytest.fail(f"fleet.voice is missing: {e}")
    cases = json.loads(BENCH.read_text(encoding="utf-8"))
    wrong = []
    for case in cases:
        cmd = voice.parse(case["say"])
        got = None if cmd is None else {"kind": cmd.kind, **{k: v for k, v in cmd.args.items() if v not in (None, "")}}
        if got != case["expect"]:
            wrong.append(f"{case['say']!r}: expected {case['expect']}, got {got}")
    accuracy = 1 - len(wrong) / len(cases)
    assert accuracy >= 0.95, f"{accuracy:.0%} right; wrong:\n" + "\n".join(wrong[:15])


def test_commands_never_contain_a_password_or_card_number():
    try:
        voice = importlib.import_module("fleet.voice")
    except ImportError as e:
        pytest.fail(f"fleet.voice is missing: {e}")
    for text in ("type my password hunter2", "wpisz hasło kotek123", "type card number 4111 1111 1111 1111"):
        cmd = voice.parse(text)
        assert cmd is None or cmd.kind == "refused", f"{text!r} must be refused, got {cmd}"
