"""The Anthropic API as a second provider: routing, request shape, cost from the price table, roles, errors."""

import json

import httpx
import pytest
from pydantic import BaseModel

from fleet import llm, models, openrouter, providers, vault

pytestmark = pytest.mark.no_db

DIRECT_OPUS = "anthropic-api/claude-opus-5"


class Answer(BaseModel):
    city: str


def message(text: str, usage: dict | None = None, stop_reason: str = "end_turn") -> dict:
    return {"id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5",
            "content": [{"type": "text", "text": text}], "stop_reason": stop_reason,
            "usage": usage or {"input_tokens": 1000, "output_tokens": 100}}


@pytest.fixture
def fake_anthropic(monkeypatch):
    """Queue up responses from a fake Messages API; inspect what Fleet sent."""
    state: dict = {"responses": [], "requests": [], "headers": []}

    def handler(request: httpx.Request) -> httpx.Response:
        state["requests"].append(json.loads(request.content) if request.content else None)
        state["headers"].append(dict(request.headers))
        status, body = state["responses"].pop(0)
        return httpx.Response(status, json=body)

    monkeypatch.setattr(providers, "_client", httpx.AsyncClient(transport=httpx.MockTransport(handler)))

    async def fake_get(name):
        return "sk-ant-test-key-123456" if name == providers.KEY_NAME else None

    monkeypatch.setattr(vault, "get", fake_get)

    async def no_sleep(_):
        return None

    monkeypatch.setattr(providers.asyncio, "sleep", no_sleep)
    return state


# -- routing ---------------------------------------------------------------------


@pytest.mark.parametrize("model_id,provider,bare", [
    ("anthropic-api/claude-opus-5", "anthropic-api", "claude-opus-5"),
    ("anthropic-api/claude-haiku-4-5", "anthropic-api", "claude-haiku-4-5"),
    ("anthropic/claude-opus-5", "openrouter", "anthropic/claude-opus-5"),
    ("typesafe/jev-1.13", "openrouter", "typesafe/jev-1.13"),
    ("perplexity/sonar-pro", "openrouter", "perplexity/sonar-pro"),
])
def test_the_prefix_decides_the_provider(model_id, provider, bare):
    assert providers.provider_of(model_id) == provider
    assert providers.is_direct(model_id) is (provider == providers.DIRECT)
    assert providers.bare(model_id) == bare  # only Fleet's own prefix is stripped
    assert providers.schema_in_the_prompt(model_id) is (provider == providers.DIRECT)


def test_only_a_model_with_a_price_can_be_used_directly():
    assert providers.priced("anthropic-api/claude-opus-5")
    assert not providers.priced("anthropic-api/claude-made-up-9")
    assert all(i.startswith(providers.PREFIX) for i in providers.direct_ids())


@pytest.fixture
def quiet_catalog(monkeypatch):
    """models.update() asks OpenRouter for its catalog; never over the network in a test."""

    async def empty():
        return {}

    monkeypatch.setattr(openrouter, "catalog", empty)


async def test_a_cheap_role_is_refused_the_claude_api_with_the_openrouter_id_to_use(quiet_catalog):
    with pytest.raises(models.ModelConfigError, match="anthropic/claude-opus-5"):
        await models.update({"research": DIRECT_OPUS})


async def test_a_claude_model_with_no_price_is_refused(quiet_catalog):
    with pytest.raises(models.ModelConfigError, match="no price"):
        await models.update({"planner": "anthropic-api/claude-made-up-9"})


# -- the request Fleet sends ----------------------------------------------------


async def test_a_structured_answer_goes_to_the_messages_api_with_the_schema_in_the_prompt(fake_anthropic):
    fake_anthropic["responses"] = [(200, message('{"city": "Warsaw"}'))]
    usage = llm.Usage()
    out = await llm.structured(model=DIRECT_OPUS, system="s", user="capital of Poland?", schema=Answer, usage=usage)
    assert out.city == "Warsaw"
    sent, headers = fake_anthropic["requests"][0], fake_anthropic["headers"][0]
    assert sent["model"] == "claude-opus-5"  # the prefix is Fleet's, not Anthropic's
    assert sent["messages"] == [{"role": "user", "content": "capital of Poland?"}]
    assert "JSON schema" in sent["system"]  # the Messages API has no response_format
    assert "response_format" not in sent and "provider" not in sent
    assert "thinking" not in sent  # unset is the one spelling every Claude model accepts
    assert headers["x-api-key"] == "sk-ant-test-key-123456"
    assert headers["anthropic-version"] == providers.ANTHROPIC_VERSION


async def test_the_system_turn_becomes_the_system_field(fake_anthropic):
    fake_anthropic["responses"] = [(200, message("ok"))]
    await llm.complete(model=DIRECT_OPUS, system="be brief", user="hello", usage=llm.Usage())
    sent = fake_anthropic["requests"][0]
    assert sent["system"] == "be brief"
    assert [m["role"] for m in sent["messages"]] == ["user"]


async def test_openrouter_only_extras_are_not_sent_to_anthropic(fake_anthropic):
    fake_anthropic["responses"] = [(200, message("ok"))]
    await llm.complete(model=DIRECT_OPUS, system="s", user="u", usage=llm.Usage(),
                       plugins=[{"id": "web"}], provider={"only": ["google-ai-studio"]})
    sent = fake_anthropic["requests"][0]
    assert "plugins" not in sent and "provider" not in sent


async def test_the_answer_is_read_from_the_text_blocks(fake_anthropic):
    body = message("ignored")
    body["content"] = [{"type": "thinking", "thinking": ""}, {"type": "text", "text": "The answer is 42."}]
    fake_anthropic["responses"] = [(200, body)]
    assert await llm.complete(model=DIRECT_OPUS, system="s", user="u", usage=llm.Usage()) == "The answer is 42."


# -- cost ------------------------------------------------------------------------


async def test_cost_comes_from_the_price_table(fake_anthropic):
    fake_anthropic["responses"] = [(200, message("ok", usage={"input_tokens": 1_000_000, "output_tokens": 1_000_000}))]
    usage = llm.Usage()
    await llm.complete(model=DIRECT_OPUS, system="s", user="u", usage=usage)
    assert usage.input_tokens == 1_000_000 and usage.output_tokens == 1_000_000
    assert usage.cost_usd == pytest.approx(5.0 + 25.0)  # claude-opus-5: $5 in / $25 out per million


async def test_cached_prompt_tokens_are_counted_in_full_and_billed_the_way_anthropic_bills_them(fake_anthropic):
    fake_anthropic["responses"] = [(200, message("ok", usage={
        "input_tokens": 0, "cache_creation_input_tokens": 1_000_000,
        "cache_read_input_tokens": 1_000_000, "output_tokens": 0}))]
    usage = llm.Usage()
    await llm.complete(model=DIRECT_OPUS, system="s", user="u", usage=usage)
    assert usage.input_tokens == 2_000_000  # the whole prompt, the way OpenRouter reports it
    assert usage.cost_usd == pytest.approx(5.0 * 1.25 + 5.0 * 0.1)  # a write costs 1.25x, a read a tenth


async def test_tokens_reported_without_a_cost_are_priced_from_the_table_too():
    """browser-use reports tokens and no money; a direct browser step must still cost what it cost."""
    usage = llm.Usage()
    await usage.add_tokens(DIRECT_OPUS, 1_000_000, 0)
    assert usage.cost_usd == pytest.approx(5.0)


# -- which roles may use it ------------------------------------------------------


def test_the_brain_may_run_on_the_claude_api_and_the_cheap_roles_may_not():
    for role in ("planner", "smart", "browser", "fast"):
        assert models.allows_provider(role, providers.DIRECT), role
    for role in ("jev", "research", "deep_research", "video"):
        assert not models.allows_provider(role, providers.DIRECT), role
    for role in models.ROLES:
        assert models.allows_provider(role, providers.OPENROUTER), role
    assert not models.allows_provider("no-such-role", providers.DIRECT)


# -- errors ----------------------------------------------------------------------


@pytest.mark.parametrize("status,phrase", [
    (401, "rejected the Claude key"),
    (403, "may not use"),
    (404, "Anthropic has no model"),
    (400, "refused the request"),
])
async def test_account_errors_fail_fast_with_advice(fake_anthropic, status, phrase):
    fake_anthropic["responses"] = [(status, {"error": {"message": "nope"}})]
    with pytest.raises(llm.LLMError, match=phrase) as exc:
        await llm.complete(model=DIRECT_OPUS, system="s", user="u", usage=llm.Usage())
    assert not exc.value.retryable
    assert len(fake_anthropic["requests"]) == 1  # no pointless retries


@pytest.mark.parametrize("status", [429, 500, 529])
async def test_busy_is_retried_then_succeeds(fake_anthropic, status):
    fake_anthropic["responses"] = [(status, {"error": {"message": "slow down"}}), (200, message("hello"))]
    assert await llm.complete(model=DIRECT_OPUS, system="s", user="u", usage=llm.Usage()) == "hello"


async def test_a_truncated_answer_is_reported(fake_anthropic):
    fake_anthropic["responses"] = [(200, message("partial", stop_reason="max_tokens"))]
    with pytest.raises(llm.LLMError, match="cut off"):
        await llm.complete(model=DIRECT_OPUS, system="s", user="u", usage=llm.Usage())


async def test_a_refusal_is_reported(fake_anthropic):
    body = message("", stop_reason="refusal")
    body["content"] = []
    fake_anthropic["responses"] = [(200, body)]
    with pytest.raises(llm.LLMError, match="declined"):
        await llm.complete(model=DIRECT_OPUS, system="s", user="u", usage=llm.Usage())


async def test_a_missing_claude_key_is_a_clear_error(monkeypatch, fake_anthropic):
    async def no_key(name):
        return None

    monkeypatch.setattr(vault, "get", no_key)
    with pytest.raises(llm.LLMError, match="Settings → API keys"):
        await llm.complete(model=DIRECT_OPUS, system="s", user="u", usage=llm.Usage())
    assert fake_anthropic["requests"] == []  # nothing was sent


async def test_an_unreadable_claude_key_is_not_retried(monkeypatch, fake_anthropic):
    async def broken(name):
        raise vault.VaultError("ANTHROPIC_API_KEY can't be decrypted")

    monkeypatch.setattr(vault, "get", broken)
    with pytest.raises(llm.LLMError, match="Paste it again") as exc:
        await llm.complete(model=DIRECT_OPUS, system="s", user="u", usage=llm.Usage())
    assert not exc.value.retryable


async def test_an_error_message_never_leaks_the_key(fake_anthropic):
    vault.remember("sk-ant-test-key-123456", providers.KEY_NAME)
    fake_anthropic["responses"] = [(400, {"error": {"message": "bad key sk-ant-test-key-123456"}})]
    with pytest.raises(llm.LLMError) as exc:
        await llm.complete(model=DIRECT_OPUS, system="s", user="u", usage=llm.Usage())
    assert "sk-ant-test-key-123456" not in str(exc.value) and "[ANTHROPIC_API_KEY]" in str(exc.value)


# -- the other provider is untouched ---------------------------------------------


async def test_an_openrouter_model_still_goes_to_openrouter(monkeypatch):
    seen = {}

    async def fake_chat(model, messages, *, usage, max_tokens=8000, **extra):
        seen["model"] = model
        return {"choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}], "usage": None}

    monkeypatch.setattr(openrouter, "chat", fake_chat)
    assert await llm.complete(model="anthropic/claude-opus-5", system="s", user="u", usage=llm.Usage()) == "hi"
    assert seen["model"] == "anthropic/claude-opus-5"
