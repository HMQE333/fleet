"""Polite, safe page fetching, shared by the fetch and scrape workers (RULES.md §6).

 - Only public web addresses: a page, a plan step or a redirect must not steer Fleet into this machine or
   the local network (Fleet's own API, AdsPower's Local API, routers, cloud metadata). Checked after DNS,
   and again on the final URL after redirects (the HTTP library also refuses redirects to internal IPs).
 - The task's domain allowlist, matched on whole domains (`evil-example.com` is not `example.com`).
 - robots.txt honoured, and requests to one site paced.
 - A blocked page is reported, never worked around: no bot-check solving, no evasion.
"""

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import lxml.etree
import lxml.html
from scrapling.fetchers import AsyncFetcher

from fleet.config import settings
from fleet.executors import Blocked
from fleet.fastbrowser import netguard

log = logging.getLogger("fleet.webfetch")

USER_AGENT_TOKEN = "FleetBot"
BLOCK_STATUSES = {401, 403, 407, 429, 503}
MAX_HTML_CHARS = 8_000_000  # a normal page is well under 1 MB; anything this big isn't worth parsing
BOT_CHECK_PHRASES = ("verify you are human", "just a moment", "enable javascript and cookies")

_robots: dict[str, RobotFileParser | None] = {}
_domain_locks: dict[str, asyncio.Lock] = {}
_domain_last: dict[str, float] = {}

# Site chrome nobody wants to read or pay for: menus, footers, sidebars, and site headers
# (a <header> inside the main content or an article is kept: it's often the product title).
_CHROME = ("//nav | //footer | //aside | //header[not(ancestor::main) and not(ancestor::article)]"
           " | //script | //style | //noscript | //svg | //iframe")


@dataclass
class Fetched:
    url: str  # the final URL, after redirects
    status: int
    html: str


def trim_chrome(html: str) -> str:
    try:
        tree = lxml.html.fromstring(html)
    except (ValueError, lxml.etree.ParserError):
        return html
    for el in tree.xpath(_CHROME):
        el.drop_tree()
    return lxml.html.tostring(tree, encoding="unicode")


def looks_like_bot_check(text: str) -> bool:
    head = text[:3000].lower()
    return any(p in head for p in BOT_CHECK_PHRASES)


async def _robots_allows(url: str) -> bool:
    parts = urlparse(url)
    origin = f"{parts.scheme}://{parts.netloc}"
    if origin not in _robots:
        parser: RobotFileParser | None = RobotFileParser()
        try:
            resp = await AsyncFetcher.get(f"{origin}/robots.txt", timeout=15, retries=1)
            if resp.status == 200:
                parser.parse(resp.body.decode("utf-8", "replace").splitlines())
            else:
                parser = None  # no robots.txt: everything allowed
        except Exception:
            parser = None
        _robots[origin] = parser
    parser = _robots[origin]
    return parser is None or parser.can_fetch(USER_AGENT_TOKEN, url)


async def _paced(domain: str) -> asyncio.Lock:
    lock = _domain_locks.setdefault(domain, asyncio.Lock())
    await lock.acquire()
    wait = settings().fetch_min_delay_seconds - (time.monotonic() - _domain_last.get(domain, 0))
    if wait > 0:
        await asyncio.sleep(wait)
    return lock


async def _check_target(url: str, allowed_domains: list[str]) -> None:
    if reason := await netguard.check_url_resolved(url):
        raise Blocked(f"Won't fetch {url}: {reason}. Fleet only reads public web pages.")
    if not netguard.host_allowed(url, allowed_domains):
        raise Blocked(f"{url} is outside this task's allowed domains ({', '.join(allowed_domains)})")


async def get(url: str, *, allowed_domains: list[str] | None = None, timeout: float = 30) -> Fetched:
    allowed = allowed_domains or []
    await _check_target(url, allowed)
    if not await _robots_allows(url):
        raise Blocked(f"robots.txt disallows fetching {url}")
    domain = urlparse(url).netloc
    lock = await _paced(domain)
    try:
        resp = await AsyncFetcher.get(url, timeout=timeout, retries=1)
    finally:
        _domain_last[domain] = time.monotonic()
        lock.release()
    final = str(getattr(resp, "url", "") or url)
    if final != url:
        await _check_target(final, allowed)  # a redirect must not lead somewhere we wouldn't have gone directly
    if resp.status in BLOCK_STATUSES:
        raise Blocked(f"{url} answered HTTP {resp.status}: likely needs a real browser")
    html = resp.html_content or ""
    if len(html) > MAX_HTML_CHARS:
        raise Blocked(f"{url} is too large to read ({len(html) // 1_000_000} MB of HTML)")
    return Fetched(url=final, status=resp.status, html=html)


def collapse(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()
