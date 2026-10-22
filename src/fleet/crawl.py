"""Crawling one section of a public site: which links to follow, and what to keep from each page.

Pure logic (no database, no network, no model) so every rule is testable. The rules, in the spirit of RULES.md §6:
 - only the start page's own site, and only paths inside the start URL's section (``/docs/`` → ``/docs/...``);
 - never files (images, archives, feeds, documents), never links marked ``rel=nofollow``;
 - addresses are normalised (no #fragment, no tracking parameters, no ``index.html``, repeated slashes collapsed) so a page is
   visited once, however it was linked;
 - a trap guard: a calendar or filter that makes endless query variants of one path is cut off.
Limits (pages, depth, time) live in the worker; the page fetch itself (robots.txt, pacing, public addresses) lives in
``fleet/webfetch.py``.
"""

import html as html_lib
import re
from collections import Counter
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit

import lxml.etree
import lxml.html

from fleet import webfetch

TRACKING_PREFIXES = ("utm_", "mc_")
TRACKING_PARAMS = {"fbclid", "gclid", "yclid", "igshid", "ref", "ref_src", "sessionid", "phpsessid", "sid"}
SKIP_SUFFIXES = (
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".ico", ".bmp", ".avif", ".pdf", ".zip", ".gz", ".tar", ".rar",
    ".7z", ".mp3", ".mp4", ".webm", ".mov", ".avi", ".mkv", ".css", ".js", ".json", ".xml", ".csv", ".xls", ".xlsx",
    ".doc", ".docx", ".ppt", ".pptx", ".exe", ".dmg", ".msi", ".apk", ".woff", ".woff2", ".ttf", ".eot", ".rss", ".atom",
)
MAX_QUERY_CHARS = 120
MAX_QUERY_VARIANTS_PER_PATH = 8
MAX_LINKS_PER_PAGE = 400
MAX_SITEMAP_FILES = 4  # the main sitemap and a few of its children
MAX_SITEMAP_URLS = 500  # pages taken from sitemaps per crawl
LOC_RE = re.compile(r"<loc>\s*(.*?)\s*</loc>", re.IGNORECASE | re.DOTALL)
INDEX_FILES = ("index.html", "index.htm", "index.php", "default.aspx")


def _site(url: str) -> str:
    return (urlsplit(url).hostname or "").lower().removeprefix("www.")


def _is_tracking(key: str) -> bool:
    key = key.lower()
    return key.startswith(TRACKING_PREFIXES) or key in TRACKING_PARAMS


def normalize(url: str) -> str | None:
    """One canonical address per page, or None when it isn't a crawlable http(s) page address."""
    try:
        parts = urlsplit((url or "").strip())
        port = parts.port
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    host = parts.hostname.lower()
    default = (parts.scheme == "http" and port == 80) or (parts.scheme == "https" and port == 443)
    netloc = host if port is None or default else f"{host}:{port}"
    path = re.sub(r"/{2,}", "/", parts.path or "/")
    for index in INDEX_FILES:
        if path.endswith("/" + index):
            path = path[: -len(index)]
    path = path or "/"  # a trailing slash is kept: servers treat /docs and /docs/ differently (relative links!)
    query = urlencode(sorted((k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                             if not _is_tracking(k)))
    if len(query) > MAX_QUERY_CHARS:
        return None
    return f"{parts.scheme}://{netloc}{path}" + (f"?{query}" if query else "")


def section_prefix(start_url: str) -> str:
    """The path a crawl stays inside: the start URL's directory (``/docs/`` or ``/docs/intro.html`` → ``/docs``);
    ``/`` means the whole site."""
    path = urlsplit(start_url).path or "/"
    if not path.endswith("/") and "." in path.rsplit("/", 1)[-1]:  # a file: its folder is the section
        path = path.rsplit("/", 1)[0]
    return path.rstrip("/") or "/"


@dataclass
class Scope:
    site: str
    prefix: str  # "/" or a path without a trailing slash
    max_depth: int

    @classmethod
    def of(cls, start_url: str, max_depth: int) -> "Scope":
        return cls(_site(start_url), section_prefix(start_url), max_depth)

    def contains(self, url: str) -> bool:
        parts = urlsplit(url)
        if _site(url) != self.site or parts.path.lower().endswith(SKIP_SUFFIXES):
            return False
        return self.prefix == "/" or parts.path == self.prefix or parts.path.startswith(self.prefix + "/")


class TrapGuard:
    """Cuts off endless query variants of one path (calendars, filters, sort orders)."""

    def __init__(self) -> None:
        self._variants: Counter = Counter()

    def allow(self, url: str) -> bool:
        parts = urlsplit(url)
        if not parts.query:
            return True
        self._variants[parts.path] += 1
        return self._variants[parts.path] <= MAX_QUERY_VARIANTS_PER_PATH


def extract_links(raw_html: str, base_url: str) -> list[str]:
    """Normalised addresses of the page's links, in page order, without duplicates and without nofollow links."""
    try:
        tree = lxml.html.fromstring(raw_html)
    except (ValueError, lxml.etree.ParserError):
        return []
    seen: dict[str, None] = {}
    for a in tree.iter("a"):
        href = (a.get("href") or "").strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:", "data:")):
            continue
        if "nofollow" in (a.get("rel") or "").lower():
            continue
        target = normalize(urljoin(base_url, href))
        if target:
            seen.setdefault(target)
        if len(seen) >= MAX_LINKS_PER_PAGE:
            break
    return list(seen)


def same_site(a: str, b: str) -> bool:
    return _site(a) == _site(b) and bool(_site(a))


def sitemap_locs(text: str) -> list[str]:
    """The addresses a sitemap (or a sitemap index) lists, in order. Read with a plain pattern, never an XML parser,
    so a hostile sitemap can't use entities or external references."""
    return [html_lib.unescape(m.group(1)).strip() for m in LOC_RE.finditer(text or "") if m.group(1).strip()]


def is_sitemap_index(text: str) -> bool:
    return bool(re.search(r"<sitemapindex\b", text or "", re.IGNORECASE))


def page_title(raw_html: str) -> str:
    try:
        tree = lxml.html.fromstring(raw_html)
    except (ValueError, lxml.etree.ParserError):
        return ""
    for xpath in ("//title", "//h1"):
        found = tree.xpath(xpath)
        if found and (text := webfetch.collapse(found[0].text_content())):
            return text[:200]
    return ""


def summarize(items: list[dict], scope: Scope, start_url: str, *, pending: int, stopped: str | None, skipped: int,
              limit_reached: str | None, from_sitemap: int = 0, preview_rows: int = 15) -> str:
    """What the planner and the operator read: how much was collected and the first pages as a table."""
    where = f"{scope.site}{'' if scope.prefix == '/' else scope.prefix}"
    lines = [f"Crawled {len(items)} page{'s' if len(items) != 1 else ''} of {where} (links within that section, up to "
             f"{scope.max_depth} clicks from {start_url}).", "Fields: url, title, depth, words, text."]
    if from_sitemap:
        lines.append(f"The site's sitemap lists {from_sitemap} more page{'s' if from_sitemap != 1 else ''} in this section "
                     "that weren't linked from the pages visited first.")
    if limit_reached:
        lines.append(f"Stopped at the {limit_reached} limit: about {pending} more pages were found but not visited. "
                     "The pages collected are kept with this step.")
    elif stopped:
        lines.append(f"Stopped early: {stopped}. What was collected before that is kept with this step.")
    if skipped:
        lines.append(f"{skipped} page{'s were' if skipped != 1 else ' was'} skipped (not found, blocked, or disallowed by robots.txt).")
    lines += ["", "| url | title | words |", "|---|---|---|"]
    for item in items[:preview_rows]:
        path = urlsplit(item["url"]).path or "/"
        lines.append(f"| {path[:70]} | {(item['title'] or '').replace('|', '/')[:60]} | {item['words']} |")
    if len(items) > preview_rows:
        lines.append(f"…and {len(items) - preview_rows} more pages (download the full table, with each page's text, from the step).")
    return "\n".join(lines)
