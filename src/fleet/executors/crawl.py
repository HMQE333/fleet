"""Crawl worker: collect the pages of ONE section of a public site (docs, a blog, a help centre) into a table.

No model is involved and nothing is spent: the section is the start URL's own folder, links are followed by code
(same site, no files, no nofollow, no query traps), every page is read through the same polite fetch as every other
worker (public addresses, robots.txt, pacing: see fleet/webfetch.py), and the limits are hard (pages, depth, time).

Progress is saved every few pages, so a step that is interrupted (a restart, a lost lease) resumes where it stopped
instead of starting over. Results go to `scrape_results` like a scrape: url, title, depth, words, text.
"""

import logging
import time
from collections import deque
from urllib.parse import urlsplit

from fleet import changes as changes_mod
from fleet import crawl, db, limits, scrape_store, webfetch
from fleet.executors import Blocked, ExecResult, RunContext
from fleet.executors.fetch import _to_markdown

log = logging.getLogger("fleet.crawl")

NAMES = ["url", "title", "depth", "words", "text"]
SAVE_EVERY = 10  # pages
MAX_SAVED_URLS = 5000  # what a saved crawl state keeps, so the row stays small


async def _cancelled(ctx: RunContext) -> bool:
    p = await db.pool()
    return await p.fetchval("select status from subtasks where id = $1", ctx.subtask["id"]) == "cancelled"


def _record(page: webfetch.Fetched, final_url: str, depth: int, text_chars: int) -> dict:
    text = _to_markdown(page.html)
    return {"url": final_url, "title": crawl.page_title(page.html), "depth": depth,
            "words": len(text.split()), "text": text[:text_chars]}


def _state(start: str, scope: crawl.Scope, frontier: deque, visited: set, *, done: bool) -> dict:
    """What the `recipe` column keeps: while running, enough to resume; when done, just the section."""
    state = {"start": start, "site": scope.site, "prefix": scope.prefix, "max_depth": scope.max_depth, "done": done}
    if not done:
        state["frontier"] = [list(f) for f in list(frontier)[:MAX_SAVED_URLS]]
        state["visited"] = list(visited)[:MAX_SAVED_URLS]
    return {"crawl": state}


async def _sitemap(ctx: RunContext, start: str, scope: crawl.Scope, allowed: list[str], visited: set[str],
                   frontier: deque, guard: crawl.TrapGuard) -> int:
    """Pages of this section that the site's sitemap lists (it may know pages nothing links to). Read politely like any
    other page (robots.txt applies), a few files at most; a missing or refused sitemap is simply not used."""
    parts = urlsplit(start)
    waiting = [f"{parts.scheme}://{parts.netloc}/sitemap.xml"]
    seen_files: set[str] = set()
    added = 0
    while waiting and len(seen_files) < crawl.MAX_SITEMAP_FILES and added < crawl.MAX_SITEMAP_URLS:
        url = waiting.pop(0)
        if url in seen_files:
            continue
        seen_files.add(url)
        try:
            page = await webfetch.get(url, allowed_domains=allowed)
        except Exception as e:  # not there, refused, robots.txt says no: carry on without it
            await ctx.log(f"No sitemap used from {url} ({e})")
            continue
        if page.status >= 400:
            continue
        locs = crawl.sitemap_locs(page.html)
        if crawl.is_sitemap_index(page.html):
            waiting += [loc for loc in locs if crawl.same_site(loc, start) and loc.lower().endswith(".xml")]
            continue
        for loc in locs:
            target = crawl.normalize(loc)
            if target and target not in visited and scope.contains(target) and guard.allow(target):
                visited.add(target)
                frontier.append((target, 1))
                added += 1
                if added >= crawl.MAX_SITEMAP_URLS:
                    break
    return added


async def run(ctx: RunContext) -> ExecResult:
    st, s = ctx.subtask, await limits.effective()
    start = crawl.normalize(st["start_url"] or "")
    if not start:
        raise ValueError("crawl subtask needs an http(s) start_url")
    allowed = ctx.task["allowed_domains"]
    scope = crawl.Scope.of(start, s.crawl_max_depth)
    deadline = time.monotonic() + s.crawl_max_seconds

    items: list[dict] = []
    frontier: deque = deque([(start, 0)])
    visited: set[str] = {start}
    saved = await scrape_store.get(st["id"])
    if saved and not ((saved["recipe"] or {}).get("crawl") or {}).get("done", True):
        state = saved["recipe"]["crawl"]
        items = list(saved["items"])
        frontier = deque((u, int(d)) for u, d in state.get("frontier", []))
        visited = set(state.get("visited", [])) | {i["url"] for i in items}
        await ctx.log(f"Resuming the crawl: {len(items)} pages already collected, {len(frontier)} waiting")

    guard = crawl.TrapGuard()
    await ctx.log(f"Crawling {scope.site}{'' if scope.prefix == '/' else scope.prefix} from {start} "
                  f"(up to {s.crawl_max_pages} pages, {scope.max_depth} clicks deep)")
    skipped = refused_in_a_row = from_sitemap = 0
    stopped = limit = None
    sitemap_due = not saved or not ((saved["recipe"] or {}).get("crawl") or {}).get("frontier")  # a resumed crawl has it

    async def save(done: bool, incomplete: bool = False, found: dict | None = None) -> None:
        await scrape_store.save(st["id"], names=NAMES, items=items, recipe=_state(start, scope, frontier, visited, done=done),
                                pages=len(items), reused=False, more_pages=incomplete or (bool(frontier) and not done),
                                changes=found)

    while frontier:
        if len(items) >= s.crawl_max_pages:
            limit = "page"
            break
        if time.monotonic() > deadline:
            stopped = "the time limit was reached"
            break
        if await _cancelled(ctx):
            stopped = "the step was cancelled"
            break
        url, depth = frontier.popleft()
        try:
            page = await webfetch.get(url, allowed_domains=allowed)
        except Blocked as e:
            skipped += 1
            refused_in_a_row += 1
            await ctx.log(f"Page not read: {e}", level="warn")
            if refused_in_a_row >= 3:
                stopped = "the site keeps refusing pages"
                break
            continue
        except Exception as e:  # one bad page must not end the crawl
            skipped += 1
            await ctx.log(f"Page couldn't be read ({e})", level="warn")
            continue
        refused_in_a_row = 0
        final = crawl.normalize(page.url) or url
        if page.status >= 400 or (final != url and (final in visited or not scope.contains(final))):
            skipped += page.status >= 400  # a redirect out of the section or to a page already seen isn't a failure
            continue
        visited.add(final)
        items.append(_record(page, final, depth, s.crawl_text_chars))
        if depth < scope.max_depth:
            for link in crawl.extract_links(page.html, page.url):
                if link not in visited and scope.contains(link) and guard.allow(link):
                    visited.add(link)
                    frontier.append((link, depth + 1))
        if sitemap_due and scope.max_depth >= 1:  # after the first page's own links, so linked pages come first
            sitemap_due = False
            from_sitemap = await _sitemap(ctx, start, scope, allowed, visited, frontier, guard)
            if from_sitemap:
                await ctx.log(f"The sitemap lists {from_sitemap} more pages in this section")
        if len(items) % SAVE_EVERY == 0:
            await save(done=False)
            await ctx.log(f"{len(items)} pages collected, {len(frontier)} waiting")

    if not items:
        raise Blocked(f"No page could be read from {start}.")
    pending = len(frontier)
    incomplete = bool(pending or stopped)
    found, report = None, ""  # what changed since the previous run of this step (saved automations only)
    if before := await scrape_store.previous(st):
        found = changes_mod.diff(before["fields"], before["items"], NAMES, items, partial=bool(before["more_pages"] or incomplete))
        report = changes_mod.describe(found, NAMES, before["updated_at"].strftime("%Y-%m-%d %H:%M UTC")) + "\n\n"
        if found["comparable"]:
            c = found["counts"]
            await ctx.log(f"{c['added']} new, {c['removed']} removed, {c['changed']} changed since the previous run",
                          type="scrape.changes", data={"counts": c, "partial": found["partial"]})
    await save(done=True, incomplete=incomplete, found=found)
    text = crawl.summarize(items, scope, start, pending=pending, stopped=stopped, skipped=skipped,
                           limit_reached=limit if pending else None, from_sitemap=from_sitemap)
    return ExecResult(result=report + text, steps=len(items))
