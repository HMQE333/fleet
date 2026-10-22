"""Which model does which job. Defaults live here; the operator overrides them in Settings.

Almost every model runs through OpenRouter. The four brain roles may also run on the Anthropic API
directly, on the operator's own Claude key: those carry `providers` below, and nothing else does
(`allows_provider`, enforced in `update`). See `fleet/providers.py` for what a direct call costs.
"""

import re

from fleet import db, openrouter, providers
from fleet.providers import DIRECT, OPENROUTER

BRAIN = (OPENROUTER, DIRECT)  # may run on the Anthropic API too

# needs: capabilities the model must have (from the OpenRouter catalog)
# providers: which APIs may run this role (default: OpenRouter only)
ROLES: dict[str, dict] = {
    "planner": {
        "label": "Planner",
        "help": "Splits your prompt into subtasks, reviews results, writes the final answer.",
        "default": "anthropic/claude-opus-5",
        "needs": ["structured_outputs"],
        "providers": BRAIN,
    },
    "smart": {
        "label": "Smart reader",
        "help": "Reads a page and extracts what's asked when it takes judgment.",
        "default": "anthropic/claude-sonnet-5",
        "needs": [],
        "providers": BRAIN,
    },
    "fast": {
        "label": "Fast reader",
        "help": "Simple extraction from a page. Cheapest.",
        "default": "anthropic/claude-haiku-4.5",
        "needs": [],
        "providers": BRAIN,
    },
    "browser": {
        "label": "Browser agent",
        "help": "Clicks and types in your AdsPower profiles. Needs image input and structured output.",
        "default": "anthropic/claude-sonnet-5",
        "needs": ["image", "structured_outputs"],
        "providers": BRAIN,
    },
    "jev": {
        "label": "Fast browser decisions (Jev)",
        "help": "Picks each click for fast browser steps through OpenRouter's decisions API — about $0.04 per "
                "million tokens. Not in OpenRouter's chat model list, so it isn't checked against it.",
        "default": "typesafe/jev-1.13",
        "needs": [],
        "offcatalog": True,
    },
    "research": {
        "label": "Research",
        "help": "Searches the web and answers with sources. Perplexity models search natively; others get OpenRouter's web search.",
        "default": "perplexity/sonar-pro",
        "needs": [],
    },
    "deep_research": {
        "label": "Deep research",
        "help": "Long multi-source reports. Slower and pricier — used only when you ask for thorough research.",
        "default": "perplexity/sonar-deep-research",
        "needs": [],
    },
    "video": {
        "label": "Video",
        "help": "Watches a video and answers about it. YouTube links need a Google Gemini model.",
        "default": "google/gemini-3.8-flash",
        "needs": ["video"],
    },
}

FAST_BROWSER_KEY = "fast_browser"


async def fast_browser_enabled() -> bool:
    """On by default: it hands anything it can't finish to the full browser agent."""
    p = await db.pool()
    value = await p.fetchval("select value from settings where key = $1", FAST_BROWSER_KEY)
    return True if value is None else bool(value.get("enabled", True))


async def set_fast_browser(enabled: bool) -> bool:
    p = await db.pool()
    await p.execute(
        "insert into settings (key, value) values ($1, $2) on conflict (key) do update set value = excluded.value, "
        "updated_at = now()",
        FAST_BROWSER_KEY, {"enabled": enabled},
    )
    return enabled


MODEL_ID_RE = re.compile(r"^[a-z0-9~][a-z0-9._\-/:~]{2,120}$")
SETTINGS_KEY = "models"


class ModelConfigError(ValueError):
    pass


def allows_provider(role: str, provider: str) -> bool:
    """Whether this role may run on that API. The brain may use the operator's own Claude key; the cheap
    roles (Jev's decisions, Perplexity's search, Gemini's video) only exist on OpenRouter."""
    return provider in ROLES.get(role, {}).get("providers", (OPENROUTER,))


async def current() -> dict[str, str]:
    p = await db.pool()
    saved = await p.fetchval("select value from settings where key = $1", SETTINGS_KEY) or {}
    return {role: saved.get(role) or spec["default"] for role, spec in ROLES.items()}


async def model_for(role: str) -> str:
    return (await current())[role]


async def update(changes: dict[str, str]) -> dict[str, str]:
    """Validate the provider and, for OpenRouter ids, the live catalog (when reachable), then save."""
    cat = await openrouter.catalog()
    for role, model_id in changes.items():
        if role not in ROLES:
            raise ModelConfigError(f"Unknown role “{role}”")
        if not MODEL_ID_RE.match(model_id):
            raise ModelConfigError(f"“{model_id}” doesn't look like a model id")
        if providers.is_direct(model_id):
            if not allows_provider(role, DIRECT):
                raise ModelConfigError(
                    f"The {ROLES[role]['label']} can't run on the Claude API — it stays on OpenRouter. "
                    f"Try “anthropic/{providers.bare(model_id)}” instead."
                )
            if not providers.priced(model_id):
                raise ModelConfigError(
                    f"Fleet has no price for “{model_id}”, so it couldn't account for what it spends on it. "
                    f"Claude models it knows: {', '.join(providers.direct_ids())}"
                )
            continue  # a direct model is not in the OpenRouter catalog; the price table is its check
        if cat and not ROLES[role].get("offcatalog"):
            m = cat.get(model_id)
            if m is None:
                raise ModelConfigError(f"OpenRouter has no model “{model_id}”")
            missing = [n for n in ROLES[role]["needs"] if n not in openrouter.capabilities(m)]
            if missing:
                raise ModelConfigError(f"{model_id} can't be the {ROLES[role]['label']}: it lacks {', '.join(missing)}")
    p = await db.pool()
    merged = {**(await p.fetchval("select value from settings where key = $1", SETTINGS_KEY) or {}), **changes}
    await p.execute(
        "insert into settings (key, value) values ($1, $2) on conflict (key) do update set value = excluded.value, updated_at = now()",
        SETTINGS_KEY, merged,
    )
    return await current()
