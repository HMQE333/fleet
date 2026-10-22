"""OpenRouter: one API key, every model (Claude, Gemini, Perplexity, ...).

Deterministic plumbing only: auth, retries, error messages a person can act on,
cost accounting (OpenRouter reports the real charge on every response), and the
model catalog used for capability checks and pricing.
"""

import asyncio
import logging
import re
import time

import httpx

from fleet import vault
from fleet.config import settings

log = logging.getLogger("fleet.openrouter")

CATALOG_TTL = 3600
RETRYABLE = {408, 429, 500, 502, 503, 504}


class LLMError(RuntimeError):
    """A model call failed. The message is written for the operator."""

    def __init__(self, message: str, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


_client: httpx.AsyncClient | None = None


def http() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            base_url=settings().openrouter_url,
            timeout=httpx.Timeout(600, connect=15),
            headers={"X-Title": "Fleet"},
        )
    return _client


async def api_key() -> str:
    try:
        key = await vault.get("OPENROUTER_API_KEY")
    except vault.VaultError as e:  # e.g. the master key changed: retrying won't help
        raise LLMError(f"Can't read the OpenRouter key: {e}. Paste it again in Settings → API keys.") from e
    if not key:
        raise LLMError("No OpenRouter API key yet — add it in Settings → API keys.")
    return key


def error_for(resp: httpx.Response, model: str) -> LLMError:
    try:
        detail = resp.json().get("error", {}).get("message") or resp.text[:300]
    except ValueError:
        detail = resp.text[:300]
    detail = vault.redact(str(detail))
    code = resp.status_code
    if code == 401:
        return LLMError("OpenRouter rejected the API key (invalid or disabled). Replace it in Settings → API keys.")
    if code == 402:
        return LLMError("Your OpenRouter account is out of credits. Top up at openrouter.ai/settings/credits.")
    if code == 403:
        return LLMError(f"OpenRouter's moderation blocked this request: {detail}")
    if code == 404:
        return LLMError(f"OpenRouter doesn't know the model “{model}”. Pick another in Settings → Models. ({detail})")
    if code in RETRYABLE:
        return LLMError(f"OpenRouter is busy or rate-limited (HTTP {code}): {detail}", retryable=True)
    return LLMError(f"OpenRouter refused the request (HTTP {code}): {detail}")


async def chat(model: str, messages: list[dict], *, usage, max_tokens: int = 8000, **extra) -> dict:
    """POST /chat/completions with retries. `extra` goes into the body (response_format, plugins, provider...)."""
    body = {"model": model, "messages": messages, "max_tokens": max_tokens, **extra}
    headers = {"Authorization": f"Bearer {await api_key()}"}
    last: LLMError | None = None
    for attempt in range(4):
        if attempt:
            await asyncio.sleep(min(2 ** attempt, 20))
        try:
            resp = await http().post("/chat/completions", json=body, headers=headers)
        except httpx.TimeoutException:
            last = LLMError(f"{model} didn't answer in time.", retryable=True)
            continue
        except httpx.TransportError as e:
            last = LLMError(f"Couldn't reach OpenRouter ({type(e).__name__}). Check the internet connection.", retryable=True)
            continue
        if resp.status_code != 200:
            last = error_for(resp, model)
            if last.retryable:
                continue
            raise last
        data = resp.json()
        await usage.add(model, data.get("usage"))
        if data.get("error"):  # upstream failure reported inside a 200
            last = LLMError(f"{model} failed upstream: {vault.redact(str(data['error'].get('message', data['error'])))}", retryable=True)
            continue
        choice = (data.get("choices") or [{}])[0]
        if choice.get("finish_reason") == "length":
            raise LLMError(f"{model}'s answer was cut off (too long). Ask for a shorter result or split the subtask.")
        return data
    assert last is not None
    raise last


def text_of(data: dict) -> str:
    content = ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    if isinstance(content, list):  # some providers return content parts
        content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
    # Some reasoning models put their scratchpad in the answer; it's not part of the result.
    return re.sub(r"^\s*<think>.*?</think>\s*", "", content, flags=re.S)


def citations_of(data: dict) -> list[str]:
    """Source URLs from web-search models (annotations), or Perplexity's top-level `citations`."""
    urls: list[str] = []
    message = (data.get("choices") or [{}])[0].get("message") or {}
    for ann in message.get("annotations") or []:
        url = (ann.get("url_citation") or {}).get("url")
        if url:
            urls.append(url)
    urls += [u for u in data.get("citations") or [] if isinstance(u, str)]
    return list(dict.fromkeys(urls))


async def key_info() -> dict:
    """What OpenRouter says about the stored key: label, credit limit, remaining, usage."""
    resp = await http().get("/key", headers={"Authorization": f"Bearer {await api_key()}"})
    if resp.status_code != 200:
        raise error_for(resp, "")
    return resp.json().get("data", {})


# -- model catalog ---------------------------------------------------------------

_catalog: dict[str, dict] = {}
_catalog_at = 0.0


async def catalog() -> dict[str, dict]:
    """All OpenRouter models by id (public endpoint, cached for an hour; stale copy kept on failure)."""
    global _catalog, _catalog_at
    if _catalog and time.monotonic() - _catalog_at < CATALOG_TTL:
        return _catalog
    try:
        resp = await http().get("/models", timeout=30)
        resp.raise_for_status()
        _catalog = {m["id"]: m for m in resp.json().get("data", [])}
        _catalog_at = time.monotonic()
    except (httpx.HTTPError, ValueError) as e:
        log.warning("could not refresh the OpenRouter model list: %s", e)
    return _catalog


def capabilities(model: dict) -> set[str]:
    arch = model.get("architecture") or {}
    caps = set(arch.get("input_modalities") or [])
    params = set(model.get("supported_parameters") or [])
    if "structured_outputs" in params:
        caps.add("structured_outputs")
    if "tools" in params:
        caps.add("tools")
    return caps


async def supports(model_id: str, need: str) -> bool | None:
    """True/False from the catalog, or None when the catalog is unavailable."""
    cat = await catalog()
    if not cat:
        return None
    m = cat.get(model_id)
    return m is not None and need in capabilities(m)


async def price_per_token(model_id: str) -> tuple[float, float]:
    m = (await catalog()).get(model_id) or {}
    p = m.get("pricing") or {}
    try:
        return float(p.get("prompt", 0)), float(p.get("completion", 0))
    except (TypeError, ValueError):
        return 0.0, 0.0
