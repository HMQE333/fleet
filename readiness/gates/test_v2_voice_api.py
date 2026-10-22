"""V2: the API has a voice surface: a command against a live session, and a task from transcribed words.

Expected shape:
- POST /api/voice/task {"text": "..."} → 201 {"task_id": ...} (the same as POST /api/tasks with the text as prompt)
- POST /api/voice/session {"url": "..."} → 201 {"session_id"} opens a local Chrome tab (voice session) for this operator
- POST /api/voice/command {"session_id", "text": "scroll down"} → 200 {"command": {...}, "result": {...}}
- commands the grammar refuses (passwords, cards) → 400 and nothing typed
- all three guarded like every other route (panel header, token when set) — tests/test_api_sweep.py covers that once
  they exist.
"""

import pytest
from httpx import ASGITransport, AsyncClient

from fleet.api import create_app

HEADERS = {"X-Fleet-Client": "panel", "Origin": "http://127.0.0.1:8710", "Host": "127.0.0.1:8710"}


@pytest.fixture
async def client():
    app = create_app(run_background=False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8710") as c:
        yield c  # the clean_db fixture migrated already; no lifespan (it would open a second LISTEN connection)


async def test_a_spoken_task_becomes_a_task(client):
    r = await client.post("/api/voice/task", json={"text": "what is the capital of Poland"}, headers=HEADERS)
    assert r.status_code == 201, r.text
    task_id = r.json()["task_id"]
    r = await client.get(f"/api/tasks/{task_id}", headers=HEADERS)
    assert r.status_code == 200 and r.json()["task"]["prompt"] == "what is the capital of Poland"  # GET answers {task, subtasks}


async def test_a_refused_command_types_nothing(client):
    r = await client.post("/api/voice/command", json={"session_id": None, "text": "type my password hunter2"}, headers=HEADERS)
    assert r.status_code == 400, r.text
    assert "hunter2" not in r.text


async def test_voice_routes_need_the_panel_header(client):
    r = await client.post("/api/voice/task", json={"text": "x"})
    assert r.status_code == 403
