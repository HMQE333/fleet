"""C2: the brain can run on the Anthropic API directly (the operator's Claude key), the hands stay on OpenRouter.

Expected shape:
- `fleet.providers` (or `fleet.llm`) routes a model id of the form `anthropic-api/<model>` (e.g. `anthropic-api/claude-opus-5`)
  to https://api.anthropic.com/v1/messages with ANTHROPIC_API_KEY (vault first, env fallback), cost from a price table;
  every other id keeps going to OpenRouter.
- `llm.structured` / `llm.complete` work unchanged for both providers (JSON schema asked for in the prompt on Anthropic,
  validated the same way).
- models.ROLES accepts `anthropic-api/...` for planner, smart, browser and fast; `jev`, `research`, `video` never.
- vault.KNOWN lists ANTHROPIC_API_KEY (optional).
"""

import importlib
import json

import pytest
from pydantic import BaseModel

from fleet import llm, vault

pytestmark = pytest.mark.no_db


class Answer(BaseModel):
    city: str


def test_the_vault_knows_the_anthropic_key():
    assert "ANTHROPIC_API_KEY" in vault.KNOWN


async def test_structured_answers_route_to_the_anthropic_api(monkeypatch):
    try:
        providers = importlib.import_module("fleet.providers")
    except ImportError as e:
        pytest.fail(f"fleet.providers is missing: {e}")
    seen = {}

    async def fake_post(url, json_body, headers):
        seen["url"], seen["body"], seen["headers"] = url, json_body, headers
        return {"content": [{"type": "text", "text": json.dumps({"city": "Warsaw"})}],
                "usage": {"input_tokens": 120, "output_tokens": 8}, "model": "claude-opus-5", "stop_reason": "end_turn"}

    monkeypatch.setattr(providers, "_post_anthropic", fake_post)

    async def key():
        return "sk-ant-test"

    monkeypatch.setattr(providers, "anthropic_key", key)
    usage = llm.Usage()
    out = await llm.structured(model="anthropic-api/claude-opus-5", system="s", user="capital of Poland?", schema=Answer, usage=usage)
    assert out.city == "Warsaw"
    assert "api.anthropic.com" in seen["url"] and seen["body"]["model"] == "claude-opus-5"
    assert "x-api-key" in {k.lower() for k in seen["headers"]}
    assert usage.input_tokens == 120 and usage.output_tokens == 8 and usage.cost_usd > 0


async def test_cheap_roles_never_accept_the_anthropic_api():
    from fleet import models

    for role in ("jev", "research", "video"):
        assert not models.allows_provider(role, "anthropic-api"), role
    for role in ("planner", "smart", "browser", "fast"):
        assert models.allows_provider(role, "anthropic-api"), role
