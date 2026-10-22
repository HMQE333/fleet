"""Which API a model id goes to, and what a call there costs.

OpenRouter routes almost everything with one key (`anthropic/claude-opus-5`, `perplexity/sonar-pro`, ...) and
reports the real charge on every answer. A model id written `anthropic-api/<model>` goes straight to the
Anthropic Messages API with the operator's own Claude key instead, for the roles that are allowed to
(`fleet/models.py`): the brain can run on the subscription the operator already pays for, while the cheap
roles — Jev's decisions, Perplexity's search, Gemini's video — stay on OpenRouter, where they exist.

Deterministic plumbing only: auth, retries, error messages a person can act on, and cost from a price table.
Anthropic bills per token and reports tokens, not money, so the table is where the money is decided — a model
Fleet has no price for can't be chosen at all.
"""

import asyncio
import logging

import httpx

from fleet import openrouter, vault
from fleet.openrouter import LLMError

log = logging.getLogger("fleet.providers")

OPENROUTER = "openrouter"
DIRECT = "anthropic-api"  # the provider, and the prefix of every model id that uses it
PREFIX = DIRECT + "/"
KEY_NAME = "ANTHROPIC_API_KEY"
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"

# USD per million tokens, input / output, from Anthropic's price list (checked Oct 2026).
PRICES_PER_M = {
    "claude-fable-5": (10.0, 50.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-opus-4-7": (5.0, 25.0),
    "claude-opus-4-6": (5.0, 25.0),
    "claude-sonnet-5": (3.0, 15.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
}
CACHE_WRITE = 1.25  # writing the prompt cache costs a quarter more than an ordinary input token
CACHE_READ = 0.1  # reading it back costs a tenth

# What the Messages API takes that Fleet might pass through. OpenRouter's own extras (response_format,
# provider, plugins) mean nothing here and are dropped rather than sent and refused.
PASS_THROUGH = {"stop_sequences", "metadata"}
RETRYABLE = openrouter.RETRYABLE | {529}  # 529: Anthropic's "overloaded"


def is_direct(model_id: str) -> bool:
    return model_id.startswith(PREFIX)


def provider_of(model_id: str) -> str:
    return DIRECT if is_direct(model_id) else OPENROUTER


def bare(model_id: str) -> str:
    """The model's own name, without Fleet's provider prefix: `anthropic-api/claude-opus-5` → `claude-opus-5`."""
    return model_id[len(PREFIX):] if is_direct(model_id) else model_id


def priced(model_id: str) -> bool:
    """Whether Fleet knows what a direct call to this model costs."""
    return bare(model_id) in PRICES_PER_M


def direct_ids() -> list[str]:
    return [PREFIX + name for name in PRICES_PER_M]


def schema_in_the_prompt(model_id: str) -> bool:
    """The Messages API has no OpenRouter-style `response_format`: the schema is asked for in the prompt."""
    return is_direct(model_id)


async def price_per_token(model_id: str) -> tuple[float, float]:
    """USD per input / output token. Anthropic from the table above, everything else from OpenRouter's catalog."""
    if not is_direct(model_id):
        return await openrouter.price_per_token(model_id)
    per_m_in, per_m_out = PRICES_PER_M.get(bare(model_id), (0.0, 0.0))
    return per_m_in / 1e6, per_m_out / 1e6


async def anthropic_key() -> str:
    try:
        key = await vault.get(KEY_NAME)
    except vault.VaultError as e:  # e.g. the master key changed: retrying won't help
        raise LLMError(f"Can't read the Claude key: {e}. Paste it again in Settings → API keys.") from e
    if not key:
        raise LLMError("No Claude API key yet — add it in Settings → API keys, or pick an OpenRouter model.")
    return key


_client: httpx.AsyncClient | None = None


def http() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=httpx.Timeout(600, connect=15))
    return _client


def error_for(resp: httpx.Response, model: str) -> LLMError:
    try:
        detail = (resp.json().get("error") or {}).get("message") or resp.text[:300]
    except ValueError:
        detail = resp.text[:300]
    detail = vault.redact(str(detail))
    code = resp.status_code
    if code == 400:
        return LLMError(f"The Claude API refused the request: {detail}")
    if code == 401:
        return LLMError("Anthropic rejected the Claude key (invalid or disabled). Replace it in Settings → API keys.")
    if code == 403:
        return LLMError(f"This Claude key may not use “{model}”: {detail}")
    if code == 404:
        return LLMError(f"Anthropic has no model “{model}”. Pick another in Settings → Models. ({detail})")
    if code == 413:
        return LLMError("The request was too large for the Claude API. Split the subtask.")
    if code == 429:
        return LLMError(f"Anthropic is rate-limiting this key: {detail}", retryable=True)
    if code in RETRYABLE:
        return LLMError(f"The Claude API is busy (HTTP {code}): {detail}", retryable=True)
    return LLMError(f"The Claude API refused the request (HTTP {code}): {detail}")


async def _post_anthropic(url: str, json_body: dict, headers: dict) -> dict:
    """One Messages request, retried while the failure is worth retrying. The answer, or an LLMError to read."""
    model = str(json_body.get("model", ""))
    last: LLMError | None = None
    for attempt in range(4):
        if attempt:
            await asyncio.sleep(min(2 ** attempt, 20))
        try:
            resp = await http().post(url, json=json_body, headers=headers)
        except httpx.TimeoutException:
            last = LLMError(f"{model} didn't answer in time.", retryable=True)
            continue
        except httpx.TransportError as e:
            last = LLMError(f"Couldn't reach the Claude API ({type(e).__name__}). Check the internet connection.",
                            retryable=True)
            continue
        if resp.status_code == 200:
            return resp.json()
        last = error_for(resp, model)
        if not last.retryable:
            raise last
    assert last is not None
    raise last


def _body(model_id: str, messages: list[dict], max_tokens: int, extra: dict) -> dict:
    """OpenRouter-shaped messages as the Messages API wants them: the system turn is a field of its own.

    `thinking` is deliberately never sent: unset is valid on every model in the price table, while each
    model family spells it differently and the wrong spelling is refused.
    """
    system = "\n\n".join(m["content"] for m in messages
                         if m["role"] == "system" and isinstance(m["content"], str))
    body: dict = {
        "model": bare(model_id),
        "max_tokens": max_tokens,
        "messages": [{"role": m["role"], "content": m["content"]} for m in messages if m["role"] != "system"],
    }
    if system:
        body["system"] = system
    dropped = sorted(k for k in extra if k not in PASS_THROUGH)
    if dropped:
        log.debug("the Claude API takes no %s; not sent", ", ".join(dropped))
    body.update({k: v for k, v in extra.items() if k in PASS_THROUGH})
    return body


def _usage_of(model_id: str, data: dict) -> dict:
    """Anthropic's token counts in the shape `llm.Usage` reads, with the cost worked out here.

    Anthropic reports tokens and never money, so this is where the price table is applied: fresh input
    tokens at full price, cache writes at 1.25x, cache reads at a tenth. The token counters stay the
    total size of the prompt, the way OpenRouter reports it.
    """
    reported = data.get("usage") or {}
    fresh = int(reported.get("input_tokens") or 0)
    written = int(reported.get("cache_creation_input_tokens") or 0)
    read = int(reported.get("cache_read_input_tokens") or 0)
    out = int(reported.get("output_tokens") or 0)
    per_m_in, per_m_out = PRICES_PER_M.get(bare(model_id), (0.0, 0.0))
    billed_in = fresh + written * CACHE_WRITE + read * CACHE_READ
    return {
        "prompt_tokens": fresh + written + read,
        "completion_tokens": out,
        "cost": (billed_in * per_m_in + out * per_m_out) / 1e6,
    }


async def chat(model_id: str, messages: list[dict], *, usage, max_tokens: int = 8000, **extra) -> dict:
    """One model call, to whichever API the id names. Returns that API's own answer."""
    if not is_direct(model_id):
        return await openrouter.chat(model_id, messages, usage=usage, max_tokens=max_tokens, **extra)
    headers = {
        "x-api-key": await anthropic_key(),
        "anthropic-version": ANTHROPIC_VERSION,
        "content-type": "application/json",
    }
    data = await _post_anthropic(ANTHROPIC_URL, _body(model_id, messages, max_tokens, extra), headers)
    await usage.add(model_id, _usage_of(model_id, data))
    stop = data.get("stop_reason")
    if stop == "max_tokens":
        raise LLMError(f"{model_id}'s answer was cut off (too long). Ask for a shorter result or split the subtask.")
    if stop == "refusal":
        raise LLMError(f"{model_id} declined to answer this request.")
    return data


def text_of(model_id: str, data: dict) -> str:
    """The answer's text. The Messages API returns content blocks; only the text blocks are the result."""
    if not is_direct(model_id):
        return openrouter.text_of(data)
    blocks = data.get("content") or []
    return "".join(b.get("text", "") for b in blocks if isinstance(b, dict) and b.get("type") == "text")
