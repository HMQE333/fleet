"""What changed between two scrapes of the same page: new, removed and edited items.

Used when a saved automation scrapes the same step again (price watching, new listings, stock). Pure code, no model:
rows are matched by a column that is unique and filled in both runs (a URL or id when there is one, else any
unique column); without such a column an edit shows up as one removed plus one added row.

Honest about what it can't know: if either run stopped early (page/item limit, a blocked page) a missing row may
simply not have been reached, so "new" and "removed" are not reported then; and if the columns differ the two
tables aren't comparable at all.
"""

import re
from collections import Counter

LIST_CAP = 50  # rows kept per list (the counts are always exact)
KEY_HINT = re.compile(r"(^|[_\s-])(url|link|href|id|sku|slug|asin|ean|code)($|[_\s-])", re.IGNORECASE)


def _text(value: object) -> str:
    return re.sub(r"\s+", " ", "" if value is None else str(value)).strip()


def pick_key(fields: list[str], old: list[dict], new: list[dict]) -> str | None:
    """A column that is filled and unique in both runs; ids and links are preferred over names."""
    def usable(name: str) -> bool:
        for rows in (old, new):
            values = [_text(r.get(name)) for r in rows]
            if not values or "" in values or len(set(values)) != len(values):
                return False
        return True

    ranked = sorted(fields, key=lambda f: (not KEY_HINT.search(f), fields.index(f)))
    return next((f for f in ranked if usable(f)), None)


def diff(fields_before: list[str], before: list[dict], fields_after: list[str], after: list[dict], *,
         partial: bool = False, soft_fields: frozenset[str] | set[str] = frozenset()) -> dict:
    """`soft_fields` are columns that weren't fully read this run (item pages beyond a cap): an edit to or from an empty
    value there says nothing about the site, so it isn't reported.

    {'comparable', 'key', 'partial', 'counts', 'added', 'removed', 'changed', 'fields_changed', 'truncated'}."""
    if sorted(fields_before) != sorted(fields_after):  # the same columns in another order still compare fine
        return {"comparable": False, "reason": "The columns are different from the previous run "
                f"({', '.join(fields_before)} before, {', '.join(fields_after)} now), so the tables can't be compared."}
    key = pick_key(fields_after, before, after)
    added: list[dict] = []
    removed: list[dict] = []
    changed: list[dict] = []
    fields_changed: Counter = Counter()
    common = 0
    if key:
        old = {_text(r[key]): r for r in before}
        new = {_text(r[key]): r for r in after}
        added = [r for k, r in new.items() if k not in old]
        removed = [r for k, r in old.items() if k not in new]
        for k in new.keys() & old.keys():
            common += 1
            edits = {f: {"from": old[k].get(f), "to": new[k].get(f)} for f in fields_after
                     if f != key and _text(old[k].get(f)) != _text(new[k].get(f))
                     and not (f in soft_fields and not (_text(old[k].get(f)) and _text(new[k].get(f))))}
            if edits:
                changed.append({"key": k, "label": _label(new[k], key, fields_after), "changes": edits})
                fields_changed.update(edits.keys())
        order = {_text(r[key]): i for i, r in enumerate(after)}
        changed.sort(key=lambda c: order[c["key"]])
    else:
        def as_row(r: dict) -> tuple:
            return tuple(_text(r.get(f)) for f in fields_after)

        left, right = Counter(map(as_row, before)), Counter(map(as_row, after))
        added_rows, removed_rows = right - left, left - right
        common = sum((left & right).values())
        for rows, bucket, wanted in ((after, added, added_rows.copy()), (before, removed, removed_rows.copy())):
            for r in rows:
                if wanted[as_row(r)] > 0:
                    wanted[as_row(r)] -= 1
                    bucket.append(r)
    if partial:  # a missing or extra row may just be one the scrape didn't reach: report edits only
        added, removed = [], []
    counts = {"added": len(added), "removed": len(removed), "changed": len(changed),
              "unchanged": common - len(changed)}
    return {
        "comparable": True, "key": key, "partial": partial, "counts": counts,
        "added": added[:LIST_CAP], "removed": removed[:LIST_CAP], "changed": changed[:LIST_CAP],
        "fields_changed": dict(fields_changed),
        "truncated": max(len(added), len(removed), len(changed)) > LIST_CAP,
    }


def _label(row: dict, key: str | None, fields: list[str]) -> str:
    name = next((_text(row.get(f)) for f in fields if f != key and _text(row.get(f))), "") if key else ""
    first = _text(row.get(key)) if key else next((_text(row.get(f)) for f in fields if _text(row.get(f))), "")
    return (name or first)[:80]


def describe(changes: dict, fields: list[str], when: str, *, per_kind: int = 8) -> str:
    """The lines the planner and the operator read in the step's result."""
    if not changes.get("comparable"):
        return f"Compared with the previous run ({when}): {changes['reason']}"
    c, key = changes["counts"], changes["key"]
    if not (c["added"] or c["removed"] or c["changed"]):
        extra = " (this run or the last stopped early, so new/removed rows weren't checked)" if changes["partial"] else ""
        return f"No changes since the previous run ({when}): {c['unchanged']} rows are the same{extra}."
    if changes["partial"]:
        head = (f"Changes since the previous run ({when}): {c['changed']} changed. New and removed rows were not "
                "checked because this run or the last one stopped early.")
    else:
        head = (f"Changes since the previous run ({when}): {c['added']} new, {c['removed']} removed, "
                f"{c['changed']} changed, {c['unchanged']} unchanged.")
    lines = [head]
    if not key:
        lines.append("(No unique column to match rows by: an edited row shows as one removed and one added.)")
    for sign, rows, total in (("+", changes["added"], c["added"]), ("-", changes["removed"], c["removed"])):
        for row in rows[:per_kind]:
            lines.append(f"{sign} {_label(row, key, fields)}")
        if total > per_kind:  # the exact count, even when the stored list is capped
            lines.append(f"{sign} …and {total - per_kind} more")
    for item in changes["changed"][:per_kind]:
        edits = "; ".join(f"{f}: {_text(v['from'])[:80]} → {_text(v['to'])[:80]}"
                          for f, v in list(item["changes"].items())[:3])
        lines.append(f"~ {item['label'] or item['key'][:60]}: {edits}")
    if c["changed"] > per_kind:
        lines.append(f"~ …and {c['changed'] - per_kind} more changed rows")
    if changes["truncated"]:
        lines.append(f"(The panel keeps the first {LIST_CAP} of each list.)")
    return "\n".join(lines)
