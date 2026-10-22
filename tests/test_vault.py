import os

import httpx
import pytest

from fleet import vault
from fleet.api import create_app

KEY_A = os.urandom(32)
KEY_B = os.urandom(32)
PANEL = {"X-Fleet-Client": "panel"}
SECRET = "sk-or-v1-0123456789abcdef-SECRET"


@pytest.mark.no_db
def test_round_trip_and_tamper_detection():
    ct, nonce = vault.encrypt("OPENROUTER_API_KEY", SECRET, KEY_A)
    assert SECRET.encode() not in ct
    assert vault.decrypt("OPENROUTER_API_KEY", ct, nonce, KEY_A) == SECRET
    with pytest.raises(vault.VaultError, match="different master key"):
        vault.decrypt("OPENROUTER_API_KEY", ct, nonce, KEY_B)
    with pytest.raises(vault.VaultError):  # ciphertext moved under another name
        vault.decrypt("ADSPOWER_API_KEY", ct, nonce, KEY_A)


async def test_values_are_encrypted_at_rest_and_never_listed():
    await vault.put("OPENROUTER_API_KEY", SECRET)
    from fleet import db

    p = await db.pool()
    raw = await p.fetchrow("select * from secrets where name = 'OPENROUTER_API_KEY'")
    assert SECRET.encode() not in bytes(raw["ciphertext"])
    listed = await vault.list_metadata()
    assert SECRET not in repr(listed)
    entry = next(s for s in listed if s["name"] == "OPENROUTER_API_KEY")
    assert entry["set"] and entry["hint"] == "…CRET"
    assert await vault.get("OPENROUTER_API_KEY") == SECRET


async def test_api_is_write_only_and_redacts_events():
    app = create_app(run_background=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8710") as c:
        r = await c.put("/api/secrets/OPENROUTER_API_KEY", json={"value": SECRET}, headers=PANEL)
        assert r.status_code == 200 and SECRET not in r.text
        r = await c.get("/api/secrets")
        assert r.status_code == 200 and SECRET not in r.text
        r = await c.put("/api/secrets/bad-name", json={"value": "x"}, headers=PANEL)
        assert r.status_code == 400
        r = await c.put("/api/secrets/OPENROUTER_API_KEY", json={"value": SECRET})  # no panel header
        assert r.status_code == 403

    from fleet import db, events

    await vault.get("OPENROUTER_API_KEY")  # the process now knows the value
    await events.emit("log", f"upstream said: bad header Bearer {SECRET}", data={"raw": [SECRET]})
    p = await db.pool()
    ev = await p.fetchrow("select message, data from events order by id desc limit 1")
    assert SECRET not in ev["message"] and SECRET not in repr(ev["data"])
    assert "[OPENROUTER_API_KEY]" in ev["message"]
