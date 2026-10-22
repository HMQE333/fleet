"""OpenRouter layer against a fake OpenRouter: formats, retries, errors, cost, sources, video routing."""

import json

import httpx
import pytest
from pydantic import BaseModel

from fleet import llm, openrouter, vault
from fleet.executors import Blocked, RunContext
from fleet.executors import video as video_worker

pytestmark = pytest.mark.no_db

CATALOG = {
    "anthropic/claude-opus-5": {"id": "anthropic/claude-opus-5", "supported_parameters": ["structured_outputs", "tools"],
                                "architecture": {"input_modalities": ["text", "image"]}, "pricing": {"prompt": "0.000005", "completion": "0.000025"}},
    "perplexity/sonar-pro": {"id": "perplexity/sonar-pro", "supported_parameters": [],
                             "architecture": {"input_modalities": ["text"]}, "pricing": {"prompt": "0.000003", "completion": "0.000015"}},
}


class Answer(BaseModel):
    city: str
    count: int


def completion(content: str, cost: float = 0.01, **extra) -> dict:
    return {"choices": [{"message": {"content": content, **extra.pop("message", {})}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20, "cost": cost}, **extra}


@pytest.fixture
def fake_openrouter(monkeypatch):
    """Queue up responses; inspect the requests Fleet sent."""
    state = {"responses": [], "requests": []}

    def handler(request: httpx.Request) -> httpx.Response:
        state["requests"].append(json.loads(request.content) if request.content else None)
        status, body = state["responses"].pop(0)
        return httpx.Response(status, json=body)

    monkeypatch.setattr(openrouter, "_client", httpx.AsyncClient(base_url="https://openrouter.test", transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(openrouter, "_catalog", CATALOG)
    monkeypatch.setattr(openrouter, "_catalog_at", 1e12)

    async def fake_get(name):
        return "sk-or-test-key-123456" if name == "OPENROUTER_API_KEY" else None

    monkeypatch.setattr(vault, "get", fake_get)

    async def no_sleep(_):
        return None

    monkeypatch.setattr(openrouter.asyncio, "sleep", no_sleep)
    return state


async def test_structured_output_uses_strict_schema_and_counts_cost(fake_openrouter):
    fake_openrouter["responses"] = [(200, completion('{"city": "Poznań", "count": 3}', cost=0.042))]
    usage = llm.Usage()
    out = await llm.structured(model="anthropic/claude-opus-5", system="s", user="u", schema=Answer, usage=usage)
    assert out == Answer(city="Poznań", count=3)
    sent = fake_openrouter["requests"][0]
    assert sent["response_format"]["json_schema"]["strict"] is True
    assert sent["response_format"]["json_schema"]["schema"]["additionalProperties"] is False
    assert sent["provider"] == {"require_parameters": True}
    assert usage.cost_usd == pytest.approx(0.042) and usage.input_tokens == 100


async def test_malformed_ai_output_gets_one_repair_round(fake_openrouter):
    fake_openrouter["responses"] = [
        (200, completion('{"city": "Poznań"}')),  # missing field
        (200, completion('```json\n{"city": "Poznań", "count": 1}\n```')),  # fenced but valid
    ]
    out = await llm.structured(model="anthropic/claude-opus-5", system="s", user="u", schema=Answer, usage=llm.Usage())
    assert out.count == 1
    repair = fake_openrouter["requests"][1]["messages"][-1]["content"]
    assert "invalid" in repair


async def test_repeated_malformed_output_fails_with_a_readable_error(fake_openrouter):
    fake_openrouter["responses"] = [(200, completion("sorry, no")), (200, completion("still no"))]
    with pytest.raises(llm.LLMError, match="wrong format"):
        await llm.structured(model="anthropic/claude-opus-5", system="s", user="u", schema=Answer, usage=llm.Usage())


async def test_models_without_structured_outputs_get_the_schema_in_the_prompt(fake_openrouter):
    fake_openrouter["responses"] = [(200, completion('{"city": "Kraków", "count": 2}'))]
    await llm.structured(model="perplexity/sonar-pro", system="s", user="u", schema=Answer, usage=llm.Usage())
    sent = fake_openrouter["requests"][0]
    assert "response_format" not in sent and "JSON schema" in sent["messages"][0]["content"]


async def test_rate_limit_is_retried_then_succeeds(fake_openrouter):
    fake_openrouter["responses"] = [(429, {"error": {"message": "slow down"}}), (200, completion("hello"))]
    assert await llm.complete(model="anthropic/claude-opus-5", system="s", user="u", usage=llm.Usage()) == "hello"


@pytest.mark.parametrize("status,phrase", [(401, "API key"), (402, "out of credits"), (404, "doesn't know the model")])
async def test_account_errors_fail_fast_with_advice(fake_openrouter, status, phrase):
    fake_openrouter["responses"] = [(status, {"error": {"message": "nope"}})]
    with pytest.raises(llm.LLMError, match=phrase) as exc:
        await llm.complete(model="anthropic/claude-opus-5", system="s", user="u", usage=llm.Usage())
    assert not exc.value.retryable
    assert len(fake_openrouter["requests"]) == 1  # no pointless retries


async def test_missing_key_is_a_clear_error(monkeypatch, fake_openrouter):
    async def no_key(name):
        return None

    monkeypatch.setattr(vault, "get", no_key)
    with pytest.raises(llm.LLMError, match="Settings → API keys"):
        await llm.complete(model="anthropic/claude-opus-5", system="s", user="u", usage=llm.Usage())


async def test_research_appends_sources(fake_openrouter):
    fake_openrouter["responses"] = [(200, completion(
        "Botland is cheapest [1].", citations=["https://botland.com.pl/rpi5"],
        message={"annotations": [{"type": "url_citation", "url_citation": {"url": "https://kamami.pl/rpi5"}}]},
    ))]
    text = await llm.research(model="perplexity/sonar-pro", system="s", user="u", usage=llm.Usage())
    assert "https://botland.com.pl/rpi5" in text and "https://kamami.pl/rpi5" in text
    assert "plugins" not in fake_openrouter["requests"][0]  # Perplexity searches natively


async def test_non_search_models_get_the_web_plugin(fake_openrouter):
    fake_openrouter["responses"] = [(200, completion("answer"))]
    await llm.research(model="anthropic/claude-opus-5", system="s", user="u", usage=llm.Usage())
    assert fake_openrouter["requests"][0]["plugins"] == [{"id": "web"}]


async def test_truncated_answer_is_reported(fake_openrouter):
    body = completion("partial")
    body["choices"][0]["finish_reason"] = "length"
    fake_openrouter["responses"] = [(200, body)]
    with pytest.raises(llm.LLMError, match="cut off"):
        await llm.complete(model="anthropic/claude-opus-5", system="s", user="u", usage=llm.Usage())


async def test_youtube_is_routed_to_gemini_on_ai_studio(fake_openrouter, monkeypatch):
    from fleet import models

    async def model_for(role):
        return "google/gemini-3.8-flash"

    monkeypatch.setattr(models, "model_for", model_for)
    fake_openrouter["responses"] = [(200, completion("A video about carp fishing."))]
    ctx = _video_ctx("https://www.youtube.com/watch?v=abc")

    async def quiet_log(*a, **k):
        return None

    monkeypatch.setattr(ctx, "log", quiet_log)
    out = await video_worker.run(ctx)
    sent = fake_openrouter["requests"][0]
    assert out.result.startswith("A video")
    assert sent["provider"] == {"only": ["google-ai-studio"]}
    assert sent["messages"][1]["content"][1] == {"type": "video_url", "video_url": {"url": "https://www.youtube.com/watch?v=abc"}}


async def test_youtube_with_a_non_gemini_model_is_refused_before_spending(fake_openrouter, monkeypatch):
    from fleet import models

    async def model_for(role):
        return "qwen/qwen3.8-flash"

    monkeypatch.setattr(models, "model_for", model_for)
    with pytest.raises(Blocked, match="Gemini"):
        await video_worker.run(_video_ctx("https://youtu.be/abc"))
    assert fake_openrouter["requests"] == []


def _video_ctx(url: str) -> RunContext:
    return RunContext(
        task={"id": None, "allowed_domains": []},
        subtask={"id": None, "task_id": None, "start_url": url, "instructions": "Summarize", "model_tier": "fast"},
        dependency_results={},
        worker_id="t",
    )


async def test_reasoning_scratchpad_is_stripped(fake_openrouter):
    fake_openrouter["responses"] = [(200, completion("<think>let me search</think>\n\nThe answer is 42."))]
    assert await llm.complete(model="anthropic/claude-opus-5", system="s", user="u", usage=llm.Usage()) == "The answer is 42."


async def test_unreadable_key_is_not_retried(fake_openrouter, monkeypatch):
    async def broken(name):
        raise vault.VaultError("OPENROUTER_API_KEY can't be decrypted")

    monkeypatch.setattr(vault, "get", broken)
    with pytest.raises(llm.LLMError, match="Paste it again") as exc:
        await llm.complete(model="anthropic/claude-opus-5", system="s", user="u", usage=llm.Usage())
    assert not exc.value.retryable


def _object_schemas(schema: dict):
    """(name, properties, required) for the top level and every $defs entry."""
    yield "root", schema["properties"], schema["required"]
    for name, sub in schema.get("$defs", {}).items():
        yield name, sub["properties"], sub["required"]


@pytest.mark.parametrize("model_name", ["Plan", "Review", "TextValue"])
def test_the_schemas_fleet_sends_ask_for_every_field_it_validates(model_name):
    """Regression: the cleanup once deleted PlannedSubtask's real `title` field, so every plan failed."""
    from fleet import planner
    from fleet.fastbrowser import decide

    model = {"Plan": planner.Plan, "Review": planner.Review, "TextValue": decide.TextValue}[model_name]
    schema = llm.strict_schema(model)
    pydantic_fields = {model.__name__: set(model.model_fields)}
    if model_name in ("Plan", "Review"):
        pydantic_fields["PlannedSubtask"] = set(planner.PlannedSubtask.model_fields)
    for name, properties, required in _object_schemas(schema):
        key = model.__name__ if name == "root" else name
        assert set(properties) == set(required) == pydantic_fields[key], name


def test_fields_named_like_schema_keywords_survive():
    class Tricky(BaseModel):
        title: str
        default: int
        description: str | None

    schema = llm.strict_schema(Tricky)
    assert set(schema["properties"]) == {"title", "default", "description"}
    assert set(schema["required"]) == {"title", "default", "description"}
    assert "title" not in schema  # pydantic's own display title is still removed


async def test_format_errors_name_the_fields(fake_openrouter):
    fake_openrouter["responses"] = [(200, completion('{"city": "Poznań"}')), (200, completion('{"count": "many"}'))]
    with pytest.raises(llm.LLMError) as e:
        await llm.structured(model="anthropic/claude-opus-5", system="s", user="u", schema=Answer, usage=llm.Usage())
    assert "city: Field required" in str(e.value) and "count:" in str(e.value)
    repair = fake_openrouter["requests"][1]["messages"][-1]["content"]
    assert "count: Field required" in repair  # the model is told exactly what to fix
