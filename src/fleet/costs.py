"""Expected cost per step, for budgeting before money is spent.

Deliberately simple and on the high side: they decide whether a step may *start* and give the
operator an estimate when reviewing a plan. The cost Fleet shows afterwards is what OpenRouter
actually charged. Measured on real runs (Oct 2026, default models): research ≈ $0.02, a page read
≈ $0.04, deep research ≈ $0.90–1.15, plan + review ≈ $0.15.
"""

# (kind, model_tier) → USD. Browser steps don't use the tier for the expensive part.
EXPECTED = {
    ("research", "fast"): 0.04,
    ("research", "smart"): 1.50,  # deep research: minutes of searching and reasoning
    ("fetch", "fast"): 0.05,
    ("fetch", "smart"): 0.12,
    ("scrape", "fast"): 0.05,  # measured $0.02 on 10 pages (one recipe call); $0 when a saved recipe is reused
    ("scrape", "smart"): 0.05,
    ("crawl", "fast"): 0.0,  # no model: plain page reads
    ("crawl", "smart"): 0.0,
    ("video", "fast"): 0.10,
    ("video", "smart"): 0.10,
    ("browser_fast", "fast"): 0.05,
    ("browser_fast", "smart"): 0.08,
    ("browser", "fast"): 0.60,  # vision model reading a screenshot every step
    ("browser", "smart"): 0.60,
}

# Kept free for the planner's final review, so workers can't spend the money needed to write the answer.
REVIEW_RESERVE = 0.15

# Plans estimated at this or more wait for the operator's OK even when they don't touch accounts.
REVIEW_PLAN_FROM_USD = 1.00


def expected(kind: str, tier: str) -> float:
    return EXPECTED.get((kind, tier), 0.10)
