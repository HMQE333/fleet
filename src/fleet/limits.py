"""Limits the operator can change in the panel (Settings → Limits) without editing .env or restarting.

Only caps that bound how much a step does: pages, depth, items, time, accounts per day. Each has a hard ceiling in code, so a
slip in the panel can't ask for an absurd crawl. What isn't here on purpose: politeness (the delay between requests to one
site, robots.txt, public-address checks) and anything that touches safety rules.

A saved value wins over the environment/default; a value of null removes the override. Executors call `effective()` once per
step and read the numbers from it like from `settings()`.
"""

from fleet import db
from fleet.config import settings

KEY = "limits"

# name -> (lowest, highest, label)
SPECS: dict[str, tuple[int, int, str]] = {
    "scrape_max_pages": (1, 100, "Scrape: pages followed per step"),
    "scrape_max_items": (10, 2000, "Scrape: items kept per step"),
    "scrape_max_details": (0, 200, "Scrape: item pages opened per step (0 = never)"),
    "scrape_max_seconds": (30, 1800, "Scrape: seconds before it stops starting pages"),
    "crawl_max_pages": (1, 500, "Crawl: pages per step"),
    "crawl_max_depth": (0, 6, "Crawl: clicks from the start page"),
    "crawl_max_seconds": (30, 1800, "Crawl: seconds before it stops starting pages"),
    "registrations_per_day": (0, 10, "Accounts Fleet may create per day (0 = none)"),
    "retention_days": (0, 3650, "Keep screenshots and step-by-step logs of finished tasks for N days (0 = forever)"),
}


class LimitError(ValueError):
    pass


class Effective:
    """`settings()` with the operator's saved limits on top: anything not overridden reads through to the real settings."""

    def __init__(self, base, saved: dict[str, int]):
        self._base, self._saved = base, saved

    def __getattr__(self, name: str):
        return self._saved[name] if name in self._saved else getattr(self._base, name)


def _valid(name: str, value: object) -> bool:
    spec = SPECS.get(name)
    return spec is not None and isinstance(value, int) and not isinstance(value, bool) and spec[0] <= value <= spec[1]


async def _saved() -> dict[str, int]:
    p = await db.pool()
    stored = await p.fetchval("select value from settings where key = $1", KEY) or {}
    return {k: v for k, v in stored.items() if _valid(k, v)}  # a hand-edited or old value out of range is ignored


async def effective() -> Effective:
    return Effective(settings(), await _saved())


async def snapshot() -> list[dict]:
    saved, base = await _saved(), settings()
    return [{"name": n, "label": label, "value": saved.get(n, getattr(base, n)), "default": getattr(base, n),
             "min": lo, "max": hi, "overridden": n in saved} for n, (lo, hi, label) in SPECS.items()]


async def put(values: dict[str, int | None]) -> list[dict]:
    """Save overrides (a number) or remove them (null). Everything is checked before anything is written."""
    for name, value in values.items():
        if name not in SPECS:
            raise LimitError(f"Unknown limit “{name}”")
        if value is not None and not _valid(name, value):
            lo, hi, label = SPECS[name]
            raise LimitError(f"{label}: a whole number from {lo} to {hi}")
    saved = await _saved()
    for name, value in values.items():
        if value is None:
            saved.pop(name, None)
        else:
            saved[name] = value
    p = await db.pool()
    await p.execute(
        "insert into settings (key, value) values ($1, $2) on conflict (key) do update set value = excluded.value, "
        "updated_at = now()", KEY, saved)
    return await snapshot()
