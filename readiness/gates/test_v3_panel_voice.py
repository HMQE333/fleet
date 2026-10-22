"""V3: the panel can listen and speak (Web Speech API; no model, no cost).

Checked statically: a Voice component exists, uses SpeechRecognition for input and speechSynthesis for readback, and is
mounted in the app shell. The operator judges how it feels (H3)."""

import pathlib
import re

import pytest

pytestmark = pytest.mark.no_db

WEB = pathlib.Path(__file__).resolve().parents[2] / "web" / "src"


def test_the_panel_has_a_voice_component_that_listens_and_speaks():
    files = list(WEB.rglob("*.tsx")) + list(WEB.rglob("*.ts"))
    text = "\n".join(f.read_text(encoding="utf-8") for f in files)
    assert re.search(r"SpeechRecognition", text), "no SpeechRecognition use in web/src"
    assert re.search(r"speechSynthesis", text), "no speechSynthesis use in web/src"
    assert re.search(r"/api/voice/", text), "the panel never calls the voice endpoints"
    app = (WEB / "App.tsx").read_text(encoding="utf-8")
    assert re.search(r"<Voice\b", app), "the Voice component is not mounted in App.tsx"


def test_the_built_panel_includes_it():
    dist = WEB.parent / "dist"
    assert dist.exists(), "web/dist missing: run npm run build"
    js = "\n".join(f.read_text(encoding="utf-8", errors="replace") for f in dist.rglob("*.js"))
    assert "speechSynthesis" in js and "/api/voice/" in js
