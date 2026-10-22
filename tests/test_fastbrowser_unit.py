"""Fast browser: network guard, approval policy, Jev request/answer handling (no browser, no network)."""

import json

import httpx
import pytest

from fleet import llm, openrouter, vault
from fleet.fastbrowser import decide, netguard, policy

pytestmark = pytest.mark.no_db


@pytest.mark.parametrize("url", [
    "http://localhost:8710/api/tasks", "http://127.0.0.1:8710/", "http://10.0.0.5/", "http://192.168.1.1/admin",
    "http://169.254.169.254/latest/meta-data", "http://[::1]/", "http://printer.local/", "http://router.lan/",
    "https://example.com:8443/", "ftp://example.com/file", "https://user:pass@example.com/",
])
def test_private_or_unsafe_urls_are_refused(url):
    assert netguard.check_url(url) is not None


def test_public_urls_pass():
    assert netguard.check_url("https://allegro.pl/listing?string=rpi") is None
    assert netguard.check_url("http://example.com:80/") is None


async def test_names_that_resolve_to_this_machine_are_refused(monkeypatch):
    async def fake_getaddrinfo(host, port, **kw):  # e.g. local.adspower.net → 127.0.0.1
        return [(2, 1, 6, "", ("127.0.0.1", 0))]

    import asyncio
    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", fake_getaddrinfo)
    reason = await netguard.check_url_resolved("http://local.adspower.net:80/api/v1/user/list")
    assert reason and "private" in reason


def test_domain_allowlist():
    assert netguard.host_allowed("https://shop.example.com/x", ["example.com"])
    assert not netguard.host_allowed("https://evil-example.com/", ["example.com"])
    assert netguard.host_allowed("https://anything.org/", [])


@pytest.mark.parametrize("label,approval,payment", [
    ("Send", True, False), ("Send message", True, False), ("Post now", True, False),
    ("Delete draft", True, False), ("Place order", True, True), ("Pay now", True, True), ("Buy now", True, True),
    ("Wyślij", True, False), ("Usuń post", True, False), ("Zapłać", True, True), ("Zamów i zapłać", True, True),
    ("Search", False, False), ("More like this", False, False), ("Order by: price", False, False),
    ("Shipping and post", False, False), ("Apply filters", False, False), ("Next page", False, False),
])
def test_approval_policy(label, approval, payment):
    assert policy.needs_approval("click", label) is approval
    assert policy.is_payment(label) is payment


def test_typing_never_needs_approval_but_the_send_button_does():
    assert not policy.needs_approval("fill", "Send to")
    assert policy.needs_approval("click", "Send")


def test_noise_links_and_duplicates_are_filtered():
    actions = [
        {"id": "e1", "kind": "click", "label": "Skip to content", "node": 1, "href": "https://x.com/#main"},
        {"id": "e2", "kind": "click", "label": "Raspberry Pi 5", "node": 2, "href": "https://x.com/p/1"},
        {"id": "e3", "kind": "click", "label": "Raspberry Pi 5 (image)", "node": 3, "href": "https://x.com/p/1#img"},
        {"id": "e4", "kind": "click", "label": "Email us", "node": 4, "href": "mailto:a@x.com"},
        {"id": "e5", "kind": "click", "label": "Log in", "node": 5, "href": "https://x.com/login"},
        {"id": "e6", "kind": "fill", "label": "Search", "node": 6},
    ]
    kept = [a["id"] for a in decide.filter_actions(actions)]
    assert kept == ["e2", "e6"]


def _page():
    return {
        "url": "https://x.com/", "title": "X", "text": "hello",
        "actions": [
            {"id": "e1", "kind": "fill", "label": "Search", "node": 1, "role": "searchbox", "value": ""},
            {"id": "e2", "kind": "click", "label": "Open Search", "node": 1, "role": "searchbox", "value": ""},
            {"id": "e3", "kind": "click", "label": "Go", "node": 2, "role": "button"},
            {"id": "scroll_down", "kind": "scroll", "label": "Scroll down", "delta": 560},
            {"id": "wait", "kind": "wait", "label": "Wait for the page to update"},
        ],
    }


def test_questions_offer_back_and_the_two_watchers():
    questions, *_rest, operations = decide.build_questions(_page(), "find x", hint="click “Go”")
    assert {"operation", "click_target", "type_text_target", "goal_done", "stuck"} <= set(questions)
    assert "BACK" in operations and "DONE" in operations and "BLOCKED" in operations
    assert questions["goal_done"]["type"] == "noul" and set(questions["goal_done"]["criteria"]) == {"true", "false"}
    assert questions["operation"]["instructions"]["recorded_next_step"] == "click “Go”"


def test_a_file_input_is_offered_to_jev_only_as_an_upload():
    page = _page()
    page["actions"].insert(0, {"id": "e0", "kind": "upload", "label": "Attachment", "node": 9, "role": "upload", "value": ""})
    questions, elements, targets, _controls, operations = decide.build_questions(page, "attach the report", None)
    assert "UPLOAD" in operations and "operator" in operations["UPLOAD"]
    assert list(targets["UPLOAD"].values())[0]["id"] == "e0"
    assert "9" not in {a["node"] for a in targets.get("TYPE_TEXT", {}).values()}
    assert next(e for e in elements if e["label"] == "Attachment")["operations"] == ["UPLOAD"]


async def test_fleets_upload_hook_takes_the_path_from_the_operators_note_and_checks_it_exists(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from fleet import hitl
    from fleet.executors import browser_fast

    doc = tmp_path / "report.txt"
    doc.write_text("x", encoding="utf-8")
    answers, logged = [], []

    async def ask(st, kind, message, **kw):
        assert kind == "help" and "Attachment" in message
        return answers.pop(0)

    async def log(message, **kw):
        logged.append(message)

    monkeypatch.setattr(hitl, "ask", ask)
    hooks = SimpleNamespace(st={"id": 1}, ctx=SimpleNamespace(log=log))
    action, state = {"label": "Attachment", "kind": "upload"}, {"url": "https://x.com/"}
    answers[:] = [{"decision": "reject"}, {"decision": "done", "note": str(tmp_path / "missing.txt")}, {"decision": "done", "note": f'"{doc}"'}]
    assert await browser_fast.FleetHooks.pick_file(hooks, action, state) is None
    assert await browser_fast.FleetHooks.pick_file(hooks, action, state) is None and "No such file" in logged[-1]
    assert await browser_fast.FleetHooks.pick_file(hooks, action, state) == str(doc.resolve())


def test_answers_outside_the_offer_are_rejected():
    with pytest.raises(llm.LLMError, match="outside the offered"):
        decide.validate_choice({"choice": "e99", "probabilities": {"e99": 1.0}}, {"e1": "x"})
    with pytest.raises(llm.LLMError):
        decide.validate_choice({"choice": "e1", "probabilities": {"e1": 7}}, {"e1": "x"})
    ok = decide.validate_choice({"choice": "e1", "probabilities": {"e1": 0.8, "junk": 0.2}, "confidence": 0.7},
                                {"e1": "x", "e2": "y"})
    assert ok["probabilities"] == {"e1": 0.8}


def test_next_best_action_skips_the_one_that_did_nothing():
    assert decide.pick_alternate({"e1": 0.6, "e2": 0.3, "back": 0.1}, {"e1"}) == "e2"
    assert decide.pick_alternate({"e1": 1.0}, {"e1"}) is None


@pytest.fixture
def fake_decisions(monkeypatch):
    sent = []

    def handler(request: httpx.Request):
        sent.append((request.url.path, json.loads(request.content)))
        body = sent_responses.pop(0)
        return httpx.Response(body[0], json=body[1])

    sent_responses: list = []
    monkeypatch.setattr(openrouter, "_client", httpx.AsyncClient(transport=httpx.MockTransport(handler)))

    async def key(name):
        return "sk-or-test-1234567890"

    monkeypatch.setattr(vault, "get", key)
    return sent, sent_responses


async def test_jev_goes_through_openrouter_decisions_and_is_costed(fake_decisions):
    sent, responses = fake_decisions
    responses.append((200, {
        "model": "typesafe/jev-1.13",
        "answers": {
            "operation": {"choice": "CLICK", "probabilities": {"CLICK": 0.9, "DONE": 0.1}, "confidence": 0.9},
            "click_target": {"choice": "2", "probabilities": {"1": 0.1, "2": 0.9}, "confidence": 0.8},
            "goal_done": {"noul": 0.05}, "stuck": {"noul": 0.02},
        },
        "usage": {"input_tokens": 2000, "output_tokens": 0},
    }))
    usage = llm.Usage()
    d = await decide.choose(_page(), "find x", [], model="typesafe/jev-1.13", usage=usage)
    path, body = sent[0]
    assert path == "/api/alpha/decisions" and body["model"] == "typesafe/jev-1.13"
    assert set(body) == {"model", "state", "questions"}
    assert d["choice"] == "e3" and d["goal_done"] == 0.05
    assert usage.cost_usd == pytest.approx(2000 * 0.042 / 1e6)


async def test_jev_unavailable_is_a_clear_non_retryable_error(fake_decisions):
    _, responses = fake_decisions
    responses.append((404, {"error": {"message": "model not found"}}))
    with pytest.raises(llm.LLMError) as e:
        await decide.choose(_page(), "find x", [], model="typesafe/jev-1.13", usage=llm.Usage())
    assert not e.value.retryable


@pytest.mark.parametrize("url,ok", [
    ("http://127.0.0.1:8710/api/tasks", False), ("http://local.adspower.net:50325/api/v1/user/list", False),
    ("http://localhost:3000/", False), ("http://printer.local/", False), ("https://allegro.pl/", True),
])
def test_full_browser_agent_is_kept_off_the_local_network_too(url, ok):
    from browser_use.browser.watchdogs.security_watchdog import SecurityWatchdog

    from fleet.executors.browser import make_session

    session = make_session("ws://127.0.0.1:1/unused", None)
    assert SecurityWatchdog(browser_session=session, event_bus=session.event_bus)._is_url_allowed(url) is ok


async def _full_agent_chat(replies):
    """The full agent's model client, with the network replaced by canned replies; returns (client, the request bodies it sent)."""
    from fleet.executors.browser import _chat

    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"id": "x", "object": "chat.completion", "created": 0, "model": "m", "usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
                                         "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": replies.pop(0)}}]})

    chat = _chat("anthropic/claude-sonnet-5", "sk-test")
    chat.http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return chat, seen


async def test_full_agent_gets_its_schema_in_the_system_prompt_not_as_a_response_format():
    """Real run: the agent's ~17 kB action schema sent as response_format made every Anthropic route answer 400 'compiled grammar is
    too large' (strict or not). Told about in the system prompt, the model answers fine, and Fleet validates the reply itself."""
    from pydantic import BaseModel

    from browser_use.llm.messages import SystemMessage, UserMessage

    class Out(BaseModel):
        ok: bool

    chat, seen = await _full_agent_chat(['{"ok": true}'])
    reply = await chat.ainvoke([SystemMessage(content="You are an agent."), UserMessage(content="hi")], Out)
    assert reply.completion.ok is True and reply.usage.prompt_tokens == 7
    body = seen[0]
    assert "response_format" not in body
    assert body["messages"][0]["role"] == "system" and body["messages"][0]["content"].startswith("You are an agent.")
    assert '"ok":{"type":"boolean"}' in body["messages"][0]["content"].replace(" ", "")  # the schema travels in the prompt
    assert body["messages"][1]["content"] == "hi"


@pytest.mark.parametrize("reply", ['{"ok": true}', '```json\n{"ok": true}\n```', 'Sure! Here is my next step:\n{"ok": true}\nHope that helps.'])
async def test_full_agent_accepts_the_json_however_the_model_wraps_it(reply):
    from pydantic import BaseModel

    from browser_use.llm.messages import UserMessage

    class Out(BaseModel):
        ok: bool

    chat, seen = await _full_agent_chat([reply])
    assert (await chat.ainvoke([UserMessage(content="hi")], Out)).completion.ok is True
    assert seen[0]["messages"][0]["role"] == "system"  # no system message from the agent: one is added for the schema


async def test_full_agent_counts_a_reply_that_does_not_fit_as_a_failed_step_and_plain_text_calls_are_untouched():
    from pydantic import BaseModel

    from browser_use.llm.exceptions import ModelProviderError
    from browser_use.llm.messages import UserMessage

    class Out(BaseModel):
        ok: bool

    chat, seen = await _full_agent_chat(['{"ok": "maybe"}', "just text"])
    with pytest.raises(ModelProviderError, match="didn't match"):
        await chat.ainvoke([UserMessage(content="hi")], Out)
    assert (await chat.ainvoke([UserMessage(content="hi")])).completion == "just text"
    assert len(seen[1]["messages"]) == 1  # a call without an output format gets no schema


# -- the full agent's click gate: code decides which clicks need the operator, not the model -----------------------------


def _gated_tools(monkeypatch, answers):
    """FleetTools whose real clicking is replaced by a recorder, plus the questions it asked the operator."""
    from types import SimpleNamespace

    from browser_use import ActionResult, Tools

    from fleet.executors.browser import FleetTools

    clicked, asked = [], []

    async def fake_act(self, action, browser_session, *a, **kw):
        clicked.append(action.model_dump(exclude_unset=True))
        return ActionResult(extracted_content="clicked")

    monkeypatch.setattr(Tools, "act", fake_act)
    tools = FleetTools()

    async def approve_click(label, url):
        asked.append((label, url))
        return answers.pop(0)

    tools.approve_click = approve_click
    labels = {1: "Submit order", 2: "Next page", 3: "Delete account"}

    class Session:
        async def get_element_by_index(self, index):
            return SimpleNamespace(attributes={}, get_all_children_text=lambda: labels[index])

        async def get_dom_element_at_coordinates(self, x, y):
            return SimpleNamespace(attributes={"aria-label": "Place order"}, get_all_children_text=lambda: "")

        async def get_current_page_url(self):
            return "https://shop.example/checkout"

    tools.set_coordinate_clicking(True)  # the agent turns this on for models that can click by position
    Model = tools.registry.create_action_model()
    return tools, Model, Session(), clicked, asked


async def test_full_agent_clicks_that_read_like_commitments_wait_for_the_operator(monkeypatch):
    tools, Model, session, clicked, asked = _gated_tools(monkeypatch, [False, True])
    refused = await tools.act(Model(click={"index": 1}), session)
    assert "did NOT approve" in refused.extracted_content and not clicked  # nothing was pressed
    assert asked == [("Submit order", "https://shop.example/checkout")]
    await tools.act(Model(click={"index": 3}), session)  # the operator says yes this time
    assert clicked == [{"click": {"index": 3}}] and asked[-1][0] == "Delete account"


async def test_full_agent_ordinary_clicks_are_not_interrupted(monkeypatch):
    tools, Model, session, clicked, asked = _gated_tools(monkeypatch, [])
    await tools.act(Model(click={"index": 2}), session)
    assert clicked == [{"click": {"index": 2}}] and asked == []


async def test_full_agent_clicks_by_position_are_checked_against_the_element_under_them(monkeypatch):
    tools, Model, session, clicked, asked = _gated_tools(monkeypatch, [False])
    refused = await tools.act(Model(click={"coordinate_x": 300, "coordinate_y": 200}), session)
    assert "did NOT approve" in refused.extracted_content and not clicked and asked[0][0] == "Place order"


async def test_an_approval_the_model_asked_for_itself_covers_only_the_next_action(monkeypatch):
    tools, Model, session, clicked, asked = _gated_tools(monkeypatch, [True])
    tools.pre_approved = True  # what the request_approval tool sets after the operator says yes
    await tools.act(Model(click={"index": 1}), session)  # that click: no second question
    assert clicked and asked == []
    tools.pre_approved = True
    await tools.act(Model(click={"index": 2}), session)  # an unrelated click uses it up…
    await tools.act(Model(click={"index": 1}), session)  # …so the next commitment asks again
    assert asked == [("Submit order", "https://shop.example/checkout")]
