"""The bill for live gates: every model call is appended to readiness/spend.jsonl and a run stops at its budget."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path


class BudgetExceeded(RuntimeError):
    pass


class Ledger:
    def __init__(self, path: Path, budget_usd: float):
        self.path = path
        self.budget_usd = budget_usd
        self.spent_this_run = 0.0

    def record(self, gate: str, what: str, cost_usd: float, **extra) -> None:
        self.spent_this_run += cost_usd
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"at": datetime.now(UTC).isoformat(timespec="seconds"), "gate": gate, "what": what,
                                "cost_usd": round(cost_usd, 6), **extra}) + "\n")
        if self.spent_this_run > self.budget_usd:
            raise BudgetExceeded(f"readiness live gates spent ${self.spent_this_run:.3f}, over the ${self.budget_usd:.2f} budget")

    def total(self) -> float:
        if not self.path.exists():
            return 0.0
        return sum(json.loads(ln).get("cost_usd", 0.0) for ln in self.path.read_text(encoding="utf-8").splitlines() if ln.strip())
