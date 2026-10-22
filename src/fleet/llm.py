"""Model calls for the planner and the reading/research/video workers.

`fleet/providers.py` decides which API each model id goes to — OpenRouter for almost everything, the
Anthropic API directly for an `anthropic-api/...` id. This layer is the same for both: AI is used for
interpretation and extraction only, and every structured answer is validated with Pydantic before
Fleet acts on it.
"""

import json
import logging
import re
from dataclasses import dataclass

from pydantic import BaseModel, ValidationError

from fleet import openrouter, providers
from fleet.openrouter import LLMError

log = logging.getLogger("fleet.llm")

__all__ = ["LLMError", "Usage", "structured", "complete", "research", "strict_schema"]


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0

    async def add(self, model: str, usage: dict | None) -> None:
        """Record one answer. OpenRouter reports the real charge in `cost`; so does the Anthropic path,
        worked out from its price table (`fleet/providers.py`). Without one, price the tokens ourselves."""
        if not usage:
            return
        inp = int(usage.get("prompt_tokens") or 0)
        out = int(usage.get("completion_tokens") or 0)
        self.input_tokens += inp
        self.output_tokens += out
        if usage.get("cost") is not None:
            self.cost_usd += float(usage["cost"])
        else:
            pin, pout = await providers.price_per_token(model)
            self.cost_usd += inp * pin + out * pout

    async def add_tokens(self, model: str, input_tokens: int, output_tokens: int) -> None:
        """For calls made by browser-use, which reports tokens but not cost."""
        await self.add(model, {"prompt_tokens": input_tokens, "completion_tokens": output_tokens})


# Schema keywords whose value maps *names* (field names, model names) to sub-schemas. Their keys are data,
# not keywords: a field called "title" or "default" must survive the cleanup below.
_NAME_MAPS = ("properties", "$defs", "definitions", "patternProperties")


def strict_schema(model: type[BaseModel]) -> dict:
    """Pydantic schema in the strict form structured outputs expect: every property required, no extras."""

    def walk(node):
        if isinstance(node, list):
            for v in node:
                walk(v)
        elif isinstance(node, dict):
            node.pop("title", None)  # pydantic's display metadata, not a field
            node.pop("default", None)
            if node.get("type") == "object" and "properties" in node:
                node["additionalProperties"] = False
                node["required"] = list(node["properties"])
            for key, value in node.items():
                if key in _NAME_MAPS and isinstance(value, dict):
                    for sub_schema in value.values():
                        walk(sub_schema)
                else:
                    walk(value)
        return node

    return walk(model.model_json_schema())


def _describe(error: Exception) -> str:
    """Field-level detail, so both the repair round and the operator see exactly what was wrong."""
    if isinstance(error, ValidationError):
        problems = [f"{'.'.join(str(p) for p in err['loc']) or 'answer'}: {err['msg']}" for err in error.errors()]
        more = f"; and {len(problems) - 5} more" if len(problems) > 5 else ""
        return "; ".join(problems[:5]) + more
    if isinstance(error, json.JSONDecodeError):
        return f"not valid JSON ({error.msg} at character {error.pos})"
    return str(error)[:300]


def _json_text(text: str) -> str:
    """Tolerate ```json fences around an otherwise valid answer."""
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    return m.group(1) if m else text.strip()


async def structured[T: BaseModel](
    *, model: str, system: str, user: str, schema: type[T], usage: Usage, max_tokens: int = 16000
) -> T:
    """Ask for an answer matching `schema`; validate it; one repair round if it doesn't match."""
    json_schema = strict_schema(schema)
    extra: dict = {}
    if providers.schema_in_the_prompt(model) or await openrouter.supports(model, "structured_outputs") is False:
        system += "\n\nReply with only a JSON object that matches this JSON schema:\n" + json.dumps(json_schema)
    else:
        extra["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": schema.__name__.lower(), "strict": True, "schema": json_schema},
        }
        extra["provider"] = {"require_parameters": True}  # only route to providers that enforce the schema
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    for attempt in range(2):
        data = await providers.chat(model, messages, usage=usage, max_tokens=max_tokens, **extra)
        text = providers.text_of(model, data)
        try:
            return schema.model_validate(json.loads(_json_text(text)))
        except (json.JSONDecodeError, ValidationError) as e:
            problem = _describe(e)
            log.warning("%s returned invalid %s (attempt %d): %s | answer began: %r", model, schema.__name__,
                        attempt + 1, problem, text[:400])
            if attempt == 1:
                raise LLMError(f"{model} kept returning an answer in the wrong format: {problem}") from e
            messages += [
                {"role": "assistant", "content": text},
                {"role": "user", "content": f"That answer was invalid: {problem}. Reply again with only the corrected JSON."},
            ]
    raise AssertionError("unreachable")


async def complete(*, model: str, system: str, user: str | list, usage: Usage, max_tokens: int = 8000, **extra) -> str:
    data = await providers.chat(
        model, [{"role": "system", "content": system}, {"role": "user", "content": user}],
        usage=usage, max_tokens=max_tokens, **extra,
    )
    text = providers.text_of(model, data)
    if not text.strip():
        raise LLMError(f"{model} returned an empty answer.", retryable=True)
    return text


async def research(*, model: str, system: str, user: str, usage: Usage, max_tokens: int = 4000) -> str:
    """Web research with sources. Perplexity searches natively; any other model gets OpenRouter's web plugin.

    Stays on OpenRouter on purpose: the web plugin and the citations are OpenRouter's, and the research
    roles are not allowed on the Anthropic API (`fleet/models.py`).
    """
    extra = {} if model.startswith("perplexity/") else {"plugins": [{"id": "web"}]}
    data = await openrouter.chat(
        model, [{"role": "system", "content": system}, {"role": "user", "content": user}],
        usage=usage, max_tokens=max_tokens, **extra,
    )
    text = openrouter.text_of(data)
    if not text.strip():
        raise LLMError(f"{model} returned an empty answer.", retryable=True)
    sources = [u for u in openrouter.citations_of(data) if u not in text]
    if sources:
        text += "\n\nSources:\n" + "\n".join(f"- {u}" for u in sources)
    return text
