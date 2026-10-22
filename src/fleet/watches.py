"""Watches: "tell me when the price drops to 150 or below" on a saved automation's scrape.

A rule is a column, a comparison and a number. When a scrape step of the automation finishes, each row whose value satisfies the rule
AND wasn't already satisfying it in the previous run is an alert: you hear about a price that just crossed your line, not about the
same cheap row every day. Numbers are read from the text of the cell by code (no model): "1 299,99 zł", "$1,299.99", "12.50 EUR".
"""

import math
import operator
import re
from uuid import UUID

from fleet import changes, db

OPS = {"<": operator.lt, "<=": operator.le, ">": operator.gt, ">=": operator.ge}
MAX_ALERTS = 50
MAX_RULES_PER_AUTOMATION = 20


class WatchError(ValueError):
    pass


def parse_number(text: object) -> float | None:
    """The first number in a cell, whatever the local format: 1 299,99 / 1.299,99 / 1,299.99 / 12.50 / 1299. None if there isn't one."""
    if text is None:
        return None
    match = re.search(r"-?\d[\d\s.,'’]*", str(text).replace(" ", " ").replace(" ", " "))
    if not match:
        return None
    raw = re.sub(r"[\s'’]", "", match.group(0)).rstrip(".,")
    if "," in raw and "." in raw:  # the separator that comes last is the decimal point
        decimal, thousands = ("," if raw.rfind(",") > raw.rfind(".") else "."), ("." if raw.rfind(",") > raw.rfind(".") else ",")
        raw = raw.replace(thousands, "").replace(decimal, ".")
    elif "," in raw or "." in raw:
        sep = "," if "," in raw else "."
        head, _, tail = raw.rpartition(sep)
        if raw.count(sep) > 1:  # only 1,234,567 style groups make a number; 3.5.2020 is a date
            if not re.fullmatch(rf"-?\d{{1,3}}(?:{re.escape(sep)}\d{{3}})+", raw):
                return None
            raw = raw.replace(sep, "")
        else:
            grouped = len(tail) == 3 and head.lstrip("-") not in ("", "0")
            raw = raw.replace(sep, "") if grouped else raw.replace(sep, ".")
    try:
        value = float(raw)
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def satisfies(op: str, value: float, threshold: float) -> bool:
    return OPS[op](value, threshold)


async def _columns(automation_id: UUID) -> dict[str, list[str]]:
    """step key -> the columns its latest result has (the automation's runs, or the task it was saved from)."""
    p = await db.pool()
    rows = await p.fetch(
        """
        select distinct on (s.key) s.key, r.fields
        from automations a
        join tasks t on t.automation_id = a.id or t.id = a.source_task_id
        join subtasks s on s.task_id = t.id join scrape_results r on r.subtask_id = s.id
        where a.id = $1 order by s.key, t.created_at desc
        """, automation_id)
    return {r["key"]: list(r["fields"]) for r in rows}


async def overview(automation_id: UUID) -> dict:
    """What the panel needs: the rules, the scrape steps that can be watched, and each step's known columns."""
    p = await db.pool()
    a = await p.fetchrow("select steps from automations where id = $1", automation_id)
    if a is None:
        raise WatchError("No such automation")
    rules = db.rows(await p.fetch(
        "select id, step_key, column_name, op, threshold, created_at from watch_rules where automation_id = $1 order by created_at",
        automation_id))
    columns = await _columns(automation_id)
    steps = [{"key": s["key"], "title": s["title"], "columns": columns.get(s["key"], [])} for s in a["steps"] if s["kind"] == "scrape"]
    return {"rules": rules, "steps": steps}


async def add(automation_id: UUID, step_key: str, column: str, op: str, threshold: float) -> dict:
    column = (column or "").strip()
    if op not in OPS:
        raise WatchError("Use one of <, <=, >, >=")
    if not column or len(column) > 60:
        raise WatchError("Name the column to watch")
    if not isinstance(threshold, (int, float)) or isinstance(threshold, bool) or not math.isfinite(threshold):
        raise WatchError("The limit must be a number")
    info = await overview(automation_id)
    step = next((s for s in info["steps"] if s["key"] == step_key), None)
    if step is None:
        raise WatchError("Only a scrape step of this automation can be watched")
    if step["columns"] and column not in step["columns"]:
        raise WatchError(f"That step has no column “{column}”. Its columns: {', '.join(step['columns'])}")
    if len(info["rules"]) >= MAX_RULES_PER_AUTOMATION:
        raise WatchError(f"At most {MAX_RULES_PER_AUTOMATION} watches per automation")
    p = await db.pool()
    row = await p.fetchrow(
        "insert into watch_rules (automation_id, step_key, column_name, op, threshold) values ($1, $2, $3, $4, $5) "
        "on conflict (automation_id, step_key, column_name, op, threshold) do update set op = excluded.op "
        "returning id, step_key, column_name, op, threshold, created_at",
        automation_id, step_key, column, op, float(threshold))
    return dict(row)


async def delete(rule_id: UUID) -> bool:
    p = await db.pool()
    return (await p.execute("delete from watch_rules where id = $1", rule_id)) == "DELETE 1"


async def alerts_for(subtask: dict, names: list[str], items: list[dict], before_items: list[dict] | None) -> list[dict]:
    """Rows of this scrape that just crossed one of the automation's watch lines (see the module docstring)."""
    p = await db.pool()
    automation_id = await p.fetchval("select automation_id from tasks where id = $1", subtask["task_id"])
    if automation_id is None:
        return []
    rules = await p.fetch(
        "select id, column_name, op, threshold from watch_rules where automation_id = $1 and step_key = $2",
        automation_id, subtask["key"])
    if not rules:
        return []
    key = changes.pick_key(names, before_items, items) if before_items else changes.pick_key(names, items, items)
    previous = {changes._text(r[key]): r for r in before_items} if key and before_items else {}
    out: list[dict] = []
    for rule in rules:
        column, op, threshold = rule["column_name"], rule["op"], rule["threshold"]
        if column not in names:
            continue
        before_matches = {tuple(changes._text(r.get(n)) for n in names) for r in (before_items or [])
                          if (v := parse_number(r.get(column))) is not None and satisfies(op, v, threshold)}
        for item in items:
            value = parse_number(item.get(column))
            if value is None or not satisfies(op, value, threshold):
                continue
            if key:
                was = previous.get(changes._text(item[key]))
                old_value = parse_number(was.get(column)) if was else None
                fresh = was is None or old_value is None or not satisfies(op, old_value, threshold)
            else:  # no column identifies a row: a matching row that wasn't matching before, as a whole
                was = None
                fresh = tuple(changes._text(item.get(n)) for n in names) not in before_matches
            if fresh:
                out.append({"rule_id": str(rule["id"]), "column": column, "op": op, "threshold": threshold,
                            "label": changes._label(item, key, names), "value": item.get(column),
                            "was": was.get(column) if was else None})
                if len(out) >= MAX_ALERTS:
                    return out
    return out


def describe(alerts: list[dict]) -> str:
    """The lines the operator and the planner read."""
    if not alerts:
        return ""
    lines = [f"Watch alerts ({len(alerts)}): rows that just crossed a line you set."]
    for a in alerts[:10]:
        was = f", was {a['was']}" if a["was"] else ""
        lines.append(f"! {a['label']}: {a['column']} {a['value']} ({a['op']} {a['threshold']:g}{was})")
    if len(alerts) > 10:
        lines.append(f"! …and {len(alerts) - 10} more")
    return "\n".join(lines)
