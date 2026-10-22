"""Scrape worker: collect many items (listings, products, rows...) from a list page and the pages after it.

A model works out how to read the page ONCE (a recipe of CSS selectors, checked by code against the real
page); every page after that — and every later run of a saved automation — is extracted by code at no AI
cost. If a saved recipe stops fitting (the site changed), it is learned again. Polite and safe like the
fetch worker (public addresses, robots.txt, pacing — see fleet/webfetch.py), with hard page/item/time caps.
"""

import logging
import time

from fleet import changes as changes_mod
from fleet import db, limits, llm, models, prompts, scrape, scrape_store, watches, webfetch
from fleet.executors import Blocked, ExecResult, RunContext

log = logging.getLogger("fleet.scrape")


async def _cancelled(ctx: RunContext) -> bool:
    p = await db.pool()
    return await p.fetchval("select status from subtasks where id = $1", ctx.subtask["id"]) == "cancelled"


async def _learn(ctx: RunContext, page: webfetch.Fetched, goal: str) -> tuple[scrape.Recipe, scrape.Extraction]:
    """Ask the model for a recipe, check it against the real page, and give it one chance to repair a bad one."""
    model = await models.model_for("smart")
    hints = scrape.pagination_hints(page.html)
    context = (f"Goal: {goal}\nPage URL: {page.url}\n\nNext-page candidates found in the page:\n"
               + ("\n".join(f"- {h}" for h in hints) or "(none found)")
               + f"\n\nPage HTML (trimmed):\n{scrape.digest(page.html)}")
    feedback = ""
    problem = "no recipe"
    for attempt in range(2):
        recipe = await llm.structured(model=model, system=prompts.SCRAPE_SYSTEM, user=context + feedback,
                                      schema=scrape.ListRecipe, usage=ctx.usage, max_tokens=2000)
        ex = scrape.extract(page.html, page.url, recipe)
        problem = scrape.judge(ex)
        if problem is None:
            stored = scrape.Recipe.model_validate(recipe.model_dump())
            if stored.detail_link_field and stored.detail_link_field not in ex.names:
                stored.detail_link_field, stored.detail_wanted = "", []  # it named a field that doesn't exist
            return stored, ex
        await ctx.log(f"Recipe attempt {attempt + 1} didn't fit the page: {problem}", level="warn")
        feedback = f"\n\nYour previous recipe was rejected: {problem}\nPrevious recipe: {recipe.model_dump_json()}"
    raise Blocked(
        f"Couldn't find a repeating list of items on {page.url} ({problem}). If the page builds its list with "
        "JavaScript, use a browser step instead.")


async def _learn_detail(ctx: RunContext, page: webfetch.Fetched, goal: str, wanted: list[str]) -> scrape.DetailRecipe | None:
    """Ask the model how to read one item's page (checked against that very page, one repair round). None = couldn't."""
    model = await models.model_for("smart")
    context = (f"Goal: {goal}\nRead from this item's page: {', '.join(wanted) or 'what the goal needs and the list lacks'}\n"
               f"Page URL: {page.url}\n\nPage HTML (trimmed):\n{scrape.digest(page.html)}")
    feedback = ""
    for attempt in range(2):
        rule = await llm.structured(model=model, system=prompts.SCRAPE_DETAIL_SYSTEM, user=context + feedback,
                                    schema=scrape.DetailRecipe, usage=ctx.usage, max_tokens=1500)
        problem = scrape.judge_detail(*scrape.extract_detail(page.html, page.url, rule))
        if problem is None:
            return rule
        await ctx.log(f"Detail recipe attempt {attempt + 1} didn't fit the item page: {problem}", level="warn")
        feedback = f"\n\nYour previous recipe was rejected: {problem}\nPrevious recipe: {rule.model_dump_json()}"
    return None


async def _details(ctx: RunContext, items: list[dict], names: list[str], recipe: scrape.Recipe, list_url: str,
                   allowed: list[str], deadline: float, goal: str) -> tuple[list[str], str, bool]:
    """Open the item pages (same site, polite, capped) and add what they show as extra columns.
    Returns (all column names, a note for the summary, whether every wanted page was read)."""
    s = await limits.effective()
    targets = [(i, u) for i, item in enumerate(items) if (u := scrape.detail_url(item.get(recipe.detail_link_field), list_url))]
    if not targets:
        return names, f"The goal needs item pages but no item had a usable link in “{recipe.detail_link_field}”.", False
    wanted = targets[: s.scrape_max_details]
    rule, learned_now, checked = recipe.detail, False, False
    done = failed = blocked = 0
    stopped = None
    columns: list[str] = list(names)
    for i, target in wanted:
        if time.monotonic() > deadline:
            stopped = "the time limit was reached"
            break
        if await _cancelled(ctx):
            stopped = "the step was cancelled"
            break
        try:
            page = await webfetch.get(target, allowed_domains=allowed)
        except Blocked as e:
            blocked += 1
            await ctx.log(f"Item page not read: {e}", level="warn")
            if blocked >= 3:
                stopped = "the site keeps refusing the item pages"
                break
            continue
        except Exception as e:
            failed += 1
            await ctx.log(f"Item page couldn't be read ({e})", level="warn")
            continue
        if rule is not None and not checked and not learned_now:  # a saved rule: free if it still fits
            checked = True
            if scrape.judge_detail(*scrape.extract_detail(page.html, page.url, rule)) is not None:
                await ctx.log("The saved item-page recipe no longer fits. Learning it again.", level="warn")
                rule = None
        if rule is None:
            rule = await _learn_detail(ctx, page, goal, recipe.detail_wanted)
            if rule is None:
                stopped = "couldn't work out how to read an item page"
                break
            recipe.detail, learned_now = rule, True
            await ctx.log(f"Learned how to read an item page: {rule.notes or ', '.join(f.name for f in rule.fields)}")
        if len(columns) == len(names):
            columns = names + scrape.detail_columns(names, [f.name for f in rule.fields])
            for row in items:
                for col in columns[len(names):]:
                    row[col] = None
        values, _ = scrape.extract_detail(page.html, page.url, rule)
        for f, col in zip(rule.fields, columns[len(names):], strict=True):
            items[i][col] = values.get(f.name)
        done += 1
    parts = [f"Opened {done} item page{'s' if done != 1 else ''} of {len(targets)} for "
             f"{', '.join(columns[len(names):]) or 'extra fields'}"]
    if len(targets) > len(wanted):
        parts.append(f"(the limit is {s.scrape_max_details} per step; the rest only have the list's fields)")
    if failed or blocked:
        parts.append(f"({failed + blocked} couldn't be read)")
    if stopped:
        parts.append(f"(stopped: {stopped})")
    complete = done == len(targets) and not stopped
    return columns, " ".join(parts) + ".", complete


async def run(ctx: RunContext) -> ExecResult:
    st, s = ctx.subtask, await limits.effective()
    url = st["start_url"]
    if not url:
        raise ValueError("scrape subtask has no start_url")
    allowed = ctx.task["allowed_domains"]
    goal = prompts.worker_context(st["instructions"], ctx.dependency_results)
    deadline = time.monotonic() + s.scrape_max_seconds

    await ctx.log(f"Scraping {url}")
    page = await webfetch.get(url, allowed_domains=allowed)
    if webfetch.looks_like_bot_check(page.html):
        raise Blocked(f"{url} showed a bot check: needs a real browser (and possibly a human)")

    if scrape.looks_empty(page.html):
        raise Blocked(f"{url} has almost no content in its HTML: its list is built by JavaScript. "
                      "Use a browser step instead.")

    recipe, extraction, reused = None, None, False
    if st.get("guide"):  # a saved automation's recipe: free if it still fits
        try:
            saved = scrape.Recipe.model_validate(st["guide"])
            extraction = scrape.extract(page.html, page.url, saved)
            if (problem := scrape.judge(extraction)) is None:
                recipe, reused = saved, True
            else:
                await ctx.log(f"The saved recipe no longer fits this page ({problem}). Learning it again.", level="warn")
        except ValueError:
            pass
    if recipe is None:
        recipe, extraction = await _learn(ctx, page, goal)
        await ctx.log(f"Learned how to read the page: {recipe.notes or recipe.item_selector}")

    names = [f.name for f in recipe.fields]
    items: list[dict] = []
    seen: set[tuple] = set()
    visited = {page.url}
    pages, more_pages, fill, stopped = 0, False, extraction.fill, None
    while True:
        pages += 1
        fresh = scrape.dedupe(extraction.items, seen)
        items += fresh
        await ctx.log(f"Page {pages}: {len(fresh)} new items ({len(items)} total)")
        if len(items) >= s.scrape_max_items:
            items, more_pages = items[: s.scrape_max_items], True
            break
        nxt = scrape.next_url(page.html, page.url, recipe)
        if not nxt or nxt in visited:
            break
        if pages >= s.scrape_max_pages or time.monotonic() > deadline:
            more_pages = True
            break
        if await _cancelled(ctx):
            break
        visited.add(nxt)
        try:
            page = await webfetch.get(nxt, allowed_domains=allowed)
        except Exception as e:  # keep what the earlier pages gave us: partial data beats none
            stopped = f"page {pages + 1} couldn't be read ({e})"
            more_pages = True
            await ctx.log(stopped.capitalize(), level="warn")
            break
        extraction = scrape.extract(page.html, page.url, recipe)
        if not extraction.items:
            await ctx.log(f"Page {pages + 1} had no items: stopping")
            break

    if not items:
        raise Blocked(f"The recipe found no items on {url}.")
    details_note, details_complete, list_names = "", True, names
    if recipe.detail_link_field and recipe.detail_link_field in names:
        names, details_note, details_complete = await _details(ctx, items, names, recipe, url, allowed, deadline, goal)
    found = None  # what changed since the previous run of this step (saved automations only)
    if before := await scrape_store.previous(st):
        found = changes_mod.diff(before["fields"], before["items"], names, items,
                                 partial=bool(before["more_pages"] or more_pages or not details_complete),
                                 soft_fields=set() if details_complete else set(names) - set(list_names))
    alerts = await watches.alerts_for(st, names, items, before["items"] if before else None)  # rows that crossed a watch line
    await scrape_store.save(st["id"], names=names, items=items, recipe=recipe.model_dump(), pages=pages,
                            reused=reused, more_pages=more_pages, changes=found, alerts=alerts)
    text = scrape.summary(names, items, pages=pages, url=url, fill=fill, reused=reused, more_pages=more_pages,
                          recipe_note=recipe.notes, stopped=stopped, details_note=details_note)
    if found:
        when = before["updated_at"].strftime("%Y-%m-%d %H:%M UTC")
        report = changes_mod.describe(found, names, when)
        text = f"{report}\n\n{text}"
        if found["comparable"]:
            c = found["counts"]
            await ctx.log(f"{c['added']} new, {c['removed']} removed, {c['changed']} changed since the previous run",
                          type="scrape.changes", data={"counts": c, "partial": found["partial"]})
    if alerts:  # the first thing the operator reads
        text = f"{watches.describe(alerts)}\n\n{text}"
        await ctx.log(f"{len(alerts)} watch alert{'s' if len(alerts) != 1 else ''}: " + "; ".join(
            f"{a['label']} {a['column']} {a['value']}" for a in alerts[:3]), type="scrape.changes", data={"alerts": len(alerts)})
    return ExecResult(result=text, steps=pages)
