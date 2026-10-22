"""The panel's Voice component (gate V3), checked statically: what it listens with, what it speaks with, and that the
words it hears go to Fleet's own voice endpoints and nowhere else."""

import pathlib
import re

import pytest

pytestmark = pytest.mark.no_db

WEB = pathlib.Path(__file__).resolve().parents[1] / "web" / "src"
VOICE = (WEB / "Voice.tsx").read_text(encoding="utf-8")
API = (WEB / "api.ts").read_text(encoding="utf-8")


def test_the_component_listens_with_the_browser_and_speaks_with_the_browser():
    assert "SpeechRecognition" in VOICE and "webkitSpeechRecognition" in VOICE
    assert "speechSynthesis" in VOICE and "SpeechSynthesisUtterance" in VOICE
    assert "pl-PL" in VOICE and "en-US" in VOICE  # both languages the grammar knows


def test_the_words_only_ever_go_to_the_voice_endpoints():
    assert "fetch(" not in VOICE and "XMLHttpRequest" not in VOICE  # every call goes through api.ts (header, token)
    calls = set(re.findall(r"api\.(\w+)\(", VOICE))
    assert calls <= {"voiceCommand", "voiceSession", "voiceTask", "approveTask", "cancelTask"}, calls
    for name, path in (("voiceTask", "/api/voice/task"), ("voiceSession", "/api/voice/session"), ("voiceCommand", "/api/voice/command")):
        assert re.search(rf"{name}:.*?\"POST\", \"{path}\"", API, re.DOTALL), name  # the method may span two lines


def test_the_api_helper_keeps_the_panel_header_for_endpoints_named_in_full():
    # request() prefixes /api unless the path already names it; either way the same headers (X-Fleet-Client, token) go out
    assert re.search(r"path\.startsWith\(\"/api/\"\) \? path : `/api\$\{path\}`", API)
    assert re.search(r"\"X-Fleet-Client\": \"panel\"", API)


def test_approve_and_reject_answer_the_inbox_unless_a_click_on_the_page_waits():
    assert "pendingClick.current = !!result.result.needs_approval" in VOICE
    assert re.search(r"if \(!pendingClick\.current && plansRef\.current\.length && \(APPROVE\.test\(words\) \|\| REJECT\.test\(words\)\)\)", VOICE)


def test_the_component_is_in_the_sidebar_once():
    app = (WEB / "App.tsx").read_text(encoding="utf-8")
    assert app.count("<Voice ") == 1 and 'import { Voice } from "./Voice";' in app
