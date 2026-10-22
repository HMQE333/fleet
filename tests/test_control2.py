"""A task can ignore the operator's "always allow" rules.

The rules under test: the switch is per task, copied by "run again" and saved automations (default on); a task with it off asks even when a
matching rule exists and does not count a use.
"""

import httpx
import pytest

from fleet import automations, db, hitl, trust
from fleet.api import create_app

from .conftest import finished, running_fleet, sub, wait_for
from .test_trust import env  # noqa: F401  (the real worker clicking "Send message")

PANEL = {"X-Fleet-Client": "panel"}


@pytest.fixture
async def client():
    app = create_app(run_background=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8710") as c:
        yield c


# -- a task that ignores the operator's rules ---------------------------------------------------------------------------------


async def test_a_task_that_ignores_always_allow_rules_asks_even_when_a_rule_matches(env):  # noqa: F811
    await trust.add(env.profile_id, "127.0.0.1", "Send message")
    task = await env.run("compose.html", "Send message")
    await env.p.execute("update tasks set use_trust_rules = false where id = $1", task["id"])
    async with running_fleet():
        card = await env.card()  # the rule exists, but this task doesn't use it
        assert card["human_request"]["kind"] == "approval" and card["human_request"]["offer"]["label"] == "Send message"
        assert await hitl.respond(card["id"], "approve")
        await wait_for(lambda: finished(task["id"]), timeout=40)
    assert (await env.rules())[0]["uses"] == 0  # not used, not counted
    again = await env.run("compose.html", "Send message")  # the default: the rule applies and nothing asks
    async with running_fleet():
        await wait_for(lambda: finished(again["id"]), timeout=40)
    assert (await env.rules())[0]["uses"] == 1


async def test_the_switch_is_stored_copied_by_run_again_and_saved_automations(client, fake):
    p = await db.pool()
    default = (await client.post("/api/tasks", json={"prompt": "just read"}, headers=PANEL)).json()
    assert default["use_trust_rules"] is True
    off = (await client.post("/api/tasks", json={"prompt": "ask me everything", "use_trust_rules": False}, headers=PANEL)).json()
    assert off["use_trust_rules"] is False
    await p.execute("update tasks set status = 'done', finished_at = now() where id = $1", off["id"])
    again = (await client.post(f"/api/tasks/{off['id']}/rerun", headers=PANEL)).json()
    assert again["use_trust_rules"] is False  # "run again" keeps the choice

    fake.plan.subtasks = [sub("s1")]
    async with running_fleet():
        await wait_for(lambda: finished(again["id"]), timeout=40)
    a = await automations.create_from_task(again["id"], "Careful", None, "UTC")
    assert a["settings"]["use_trust_rules"] is False
    run = await automations.start_run(a["id"])
    assert run["use_trust_rules"] is False  # and so does every run of the automation
    old = {k: v for k, v in a["settings"].items() if k != "use_trust_rules"}  # an automation saved before the switch existed
    await p.execute("update automations set settings = $2 where id = $1", a["id"], old)
    await p.execute("update tasks set status = 'done' where id = $1", run["id"])
    assert (await automations.start_run(a["id"]))["use_trust_rules"] is True
