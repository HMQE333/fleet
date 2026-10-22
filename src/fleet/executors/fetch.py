"""Fetch worker: read one public URL with Scrapling, then extract what the instructions ask for.

Polite and safe by design (RULES.md §6, see fleet/webfetch.py): public addresses only, robots.txt
honoured, requests paced, and it never tries to solve a bot check — a blocked page is reported back
so the planner can switch to a browser subtask on a real profile.
"""

import logging
import re

from markdownify import markdownify

from fleet import llm, models, prompts, webfetch
from fleet.executors import Blocked, ExecResult, RunContext

log = logging.getLogger("fleet.fetch")

MAX_PAGE_CHARS = 40_000  # ~10k tokens; after trimming, product and article pages fit easily


def _to_markdown(html: str) -> str:
    html = webfetch.trim_chrome(html)
    html = re.sub(r"<(script|style|noscript|svg)[\s\S]*?</\1>", "", html, flags=re.I)
    md = markdownify(html, heading_style="ATX")
    md = re.sub(r"\n{3,}", "\n\n", md)
    return md.strip()


async def fetch_markdown(url: str, allowed_domains: list[str] | None = None) -> tuple[str, int]:
    page = await webfetch.get(url, allowed_domains=allowed_domains)
    md = _to_markdown(page.html)
    if webfetch.looks_like_bot_check(md):
        raise Blocked(f"{url} showed a bot check: needs a real browser (and possibly a human)")
    return md, page.status


async def run(ctx: RunContext) -> ExecResult:
    st = ctx.subtask
    url = st["start_url"]
    if not url:
        raise ValueError("fetch subtask has no start_url")
    await ctx.log(f"Fetching {url}")
    md, status = await fetch_markdown(url, ctx.task["allowed_domains"])
    truncated = len(md) > MAX_PAGE_CHARS
    md = md[:MAX_PAGE_CHARS]
    await ctx.log(f"Got {len(md):,} chars (HTTP {status}){' — truncated' if truncated else ''}")
    if len(md) < 200:
        await ctx.log("Very little text on the page — it may need JavaScript (use a browser subtask)", level="warn")
    user = (
        f"{prompts.worker_context(st['instructions'], ctx.dependency_results)}\n\n"
        f"Page URL: {url}\n<page>\n{md}\n</page>"
    )
    model = await models.model_for("smart" if st["model_tier"] == "smart" else "fast")
    answer = await llm.complete(model=model, system=prompts.FETCH_SYSTEM, user=user, usage=ctx.usage)
    return ExecResult(result=answer)
