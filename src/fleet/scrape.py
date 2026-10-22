"""Structured scraping: a model works out ONCE how a page's repeating items can be read (CSS selectors,
a "recipe"); code then extracts every page with it, deterministically and at no AI cost.

This module is pure logic (no database, no network, no model calls) so every rule is testable:
 - the recipe model the AI fills in, and the prompt context built from a page (a trimmed digest plus
   pagination hints taken from the untrimmed page);
 - extraction with lxml + cssselect — selectors are only ever *matched*, never executed as code;
 - validation: a recipe is only trusted when it really finds a list (several items, mostly-filled fields);
 - next-page links (same site only), de-duplication, CSV with spreadsheet-formula neutralisation.
"""

import csv
import io
import re
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit

import cssselect
import lxml.etree
import lxml.html
from pydantic import BaseModel

from fleet import webfetch

MAX_VALUE_CHARS = 500
DIGEST_CHARS = 30_000
MAX_CONTAINERS_PER_PAGE = 500
CSV_DANGEROUS_START = ("=", "+", "-", "@", "\t", "\r")

KEEP_ATTRS = {
    "class", "id", "href", "src", "alt", "title", "aria-label", "itemprop", "itemtype", "rel", "role", "type",
    "name", "value", "content", "datetime", "data-score", "data-price", "data-id", "data-sku",
}
NEXT_RE = re.compile(r"\b(next|older|more results|następna|nastepna|dalej|weiter|suivant|siguiente)\b|[›»>]", re.I)


class FieldRule(BaseModel):
    name: str  # snake_case column name, e.g. price
    selector: str  # CSS selector relative to the item element; "" = the item element itself
    attribute: str  # "text" for visible text, or an attribute name like href, src, data-price, content


class ListRecipe(BaseModel):
    """What the model fills in for a list page."""

    item_selector: str  # matches one element per item, and every item on the page
    fields: list[FieldRule]
    next_selector: str  # CSS selector of the "next page" link; "" when the list isn't paginated
    notes: str  # one line: what was learned about this page (shown to the operator)
    # Only when the goal needs more than the list shows: the field holding each item's own page, and what to read there.
    detail_link_field: str = ""
    detail_wanted: list[str] = []


class DetailRecipe(BaseModel):
    """What the model fills in for one item's own page: single values, selectors relative to the whole page."""

    fields: list[FieldRule]  # a selector of "" means the whole page
    notes: str


class Recipe(ListRecipe):
    """A list recipe plus (once learned) how to read the item pages. This is what is stored and replayed."""

    detail: DetailRecipe | None = None


@dataclass
class Extraction:
    items: list[dict]
    containers: int  # elements matched by item_selector
    fill: dict[str, float]  # field name -> share of containers where it was found
    error: str | None = None  # a selector the page couldn't be queried with
    dropped_empty: int = 0
    names: list[str] = field(default_factory=list)


# -- what the model gets to see --------------------------------------------------------------------------


def _snippet(el, limit: int = 240) -> str:
    path, node = [], el.getparent()
    while node is not None and len(path) < 3:
        cls = ".".join((node.get("class") or "").split()[:2])
        path.append(f"{node.tag}{'.' + cls if cls else ''}")
        node = node.getparent()
    text = lxml.html.tostring(el, encoding="unicode", with_tail=False)
    return f"{' < '.join(path)} :: {re.sub(r'\s+', ' ', text)[:limit]}"


def pagination_hints(raw_html: str, limit: int = 12) -> list[str]:
    """Next-page candidates, from the UNtrimmed page (site navigation, where pagination often lives, is trimmed
    from the digest). Never returns more than `limit` short snippets."""
    try:
        tree = lxml.html.fromstring(raw_html)
    except (ValueError, lxml.etree.ParserError):
        return []
    found: list[str] = []

    def add(el):
        snippet = _snippet(el)
        if snippet not in found:
            found.append(snippet)

    for el in tree.xpath("//a[@rel='next'] | //link[@rel='next']"):
        add(el)
    for a in tree.xpath("//a[@href]"):
        label = " ".join([a.text_content() or "", a.get("aria-label") or "", a.get("title") or "", a.get("class") or ""])
        if NEXT_RE.search(label):
            add(a)
    for box in tree.xpath("//*[contains(@class, 'pagination') or contains(@class, 'pager') or contains(@class, 'paging')]"):
        for a in box.iter("a"):
            add(a)
    return found[:limit]


def digest(raw_html: str, limit: int = DIGEST_CHARS) -> str:
    """The page as the model needs it to find repeating structure: no menus/footers/scripts, short attribute
    list, short text. Cut at `limit` characters (a few items are enough to see the pattern)."""
    html = webfetch.trim_chrome(raw_html)
    try:
        tree = lxml.html.fromstring(html)
    except (ValueError, lxml.etree.ParserError):
        return html[:limit]
    for comment in tree.xpath("//comment()"):
        comment.drop_tree()
    for el in tree.iter():
        if not isinstance(el.tag, str):
            continue
        for name in list(el.attrib):
            if name not in KEEP_ATTRS:
                del el.attrib[name]
            else:
                value = el.attrib[name]
                el.attrib[name] = "" if value.startswith("data:") else value[:120]
        if el.text:
            el.text = re.sub(r"\s+", " ", el.text)[:160]
        if el.tail:
            el.tail = re.sub(r"\s+", " ", el.tail)[:160]
    return lxml.html.tostring(tree, encoding="unicode")[:limit]


def looks_empty(raw_html: str, *, minimum_chars: int = 200) -> bool:
    """A page whose HTML holds almost no text is a JavaScript shell: its list is built in a real browser."""
    try:
        text = lxml.html.fromstring(webfetch.trim_chrome(raw_html)).text_content()
    except (ValueError, lxml.etree.ParserError):
        return True
    return len(webfetch.collapse(text)) < minimum_chars


# -- extraction --------------------------------------------------------------------------------------------


def _value(container, rule: FieldRule, base_url: str) -> str | None:
    element = container
    if rule.selector.strip():
        found = container.cssselect(rule.selector)
        if not found:
            return None
        element = found[0]
    if rule.attribute == "text":
        value = webfetch.collapse(element.text_content())
    else:
        value = (element.get(rule.attribute) or "").strip()
        if value and rule.attribute in ("href", "src", "data-src"):
            value = urljoin(base_url, value)
    return value[:MAX_VALUE_CHARS] or None


def extract(raw_html: str, base_url: str, recipe: Recipe) -> Extraction:
    names = [f.name for f in recipe.fields]
    try:
        tree = lxml.html.fromstring(raw_html)
        containers = tree.cssselect(recipe.item_selector)[:MAX_CONTAINERS_PER_PAGE]
        rows = [{f.name: _value(c, f, base_url) for f in recipe.fields} for c in containers]
    except (cssselect.SelectorError, ValueError, lxml.etree.ParserError) as e:
        return Extraction([], 0, {n: 0.0 for n in names}, error=f"{type(e).__name__}: {e}", names=names)
    kept = [r for r in rows if any(v is not None for v in r.values())]
    fill = {n: (sum(r[n] is not None for r in rows) / len(rows) if rows else 0.0) for n in names}
    return Extraction(kept, len(containers), fill, dropped_empty=len(rows) - len(kept), names=names)


def judge(ex: Extraction, *, min_items: int = 2) -> str | None:
    """None when the recipe really found a list; otherwise what's wrong, worded for the model's repair round."""
    if ex.error:
        return f"A selector couldn't be used on the page ({ex.error}). Use plain CSS selectors only."
    if ex.containers < min_items:
        return (f"item_selector matched {ex.containers} element(s). It must match EVERY item on the page, "
                f"one element per item, and at least {min_items}.")
    if not ex.fill or max(ex.fill.values()) < 0.8 or sum(ex.fill.values()) / len(ex.fill) < 0.5:
        shares = ", ".join(f"{n} {round(v * 100)}%" for n, v in ex.fill.items())
        return f"Most fields came back empty on the {ex.containers} matched items ({shares}). Fix the field selectors."
    return None


def extract_detail(raw_html: str, base_url: str, rule: DetailRecipe) -> tuple[dict[str, str | None], str | None]:
    """The values of one item's page, and the problem if a selector couldn't be used."""
    try:
        tree = lxml.html.fromstring(raw_html)
        return {f.name: _value(tree, f, base_url) for f in rule.fields}, None
    except (cssselect.SelectorError, ValueError, lxml.etree.ParserError) as e:
        return {f.name: None for f in rule.fields}, f"{type(e).__name__}: {e}"


def judge_detail(values: dict[str, str | None], error: str | None) -> str | None:
    """None when the detail recipe read the sample page properly; otherwise what is wrong, for the repair round."""
    if error:
        return f"A selector couldn't be used on the page ({error}). Use plain CSS only."
    found = sum(v is not None for v in values.values())
    if not values or found == 0 or found < len(values) / 2:
        shown = ", ".join(f"{n}: {'found' if v is not None else 'EMPTY'}" for n, v in values.items())
        return f"Only {found} of {len(values)} fields were found on this page ({shown}). Fix the selectors of the empty ones."
    return None


def detail_url(value: object, list_url: str) -> str | None:
    """An item's own page: an http(s) address on the same site as the list, and not the list itself."""
    if not isinstance(value, str) or not value.strip():
        return None
    target = urljoin(list_url, value.strip()).split("#")[0]
    if urlsplit(target).scheme not in ("http", "https") or _site(target) != _site(list_url):
        return None
    return None if target == list_url.split("#")[0] else target


def detail_columns(names: list[str], detail_names: list[str]) -> list[str]:
    """Column names for what the item pages add; a name the list already uses gets a detail_ prefix."""
    taken, out = set(names), []
    for n in detail_names:
        col = n if n not in taken else f"detail_{n}"
        while col in taken:
            col += "_"
        taken.add(col)
        out.append(col)
    return out


def next_url(raw_html: str, base_url: str, recipe: Recipe) -> str | None:
    """The next page's address, or None. Only links within the same site are followed."""
    if not recipe.next_selector.strip():
        return None
    try:
        tree = lxml.html.fromstring(raw_html)
        found = tree.cssselect(recipe.next_selector)
    except (cssselect.SelectorError, ValueError, lxml.etree.ParserError):
        return None
    for el in found:
        link = el if el.tag == "a" else next(iter(el.iter("a")), None)
        href = (link.get("href") if link is not None else el.get("href")) or ""
        if not href or href.startswith(("#", "javascript:")):
            continue
        target = urljoin(base_url, href)
        if _site(target) == _site(base_url) and target.split("#")[0] != base_url.split("#")[0]:
            return target.split("#")[0]
    return None


def _site(url: str) -> str:
    return (urlsplit(url).hostname or "").lower().removeprefix("www.")


def dedupe(rows: list[dict], seen: set[tuple]) -> list[dict]:
    fresh = []
    for row in rows:
        key = tuple(row.values())
        if key not in seen:
            seen.add(key)
            fresh.append(row)
    return fresh


# -- presenting results ----------------------------------------------------------------------------------


def _cell(value: object, limit: int | None = None) -> str:
    text = "" if value is None else str(value).replace("\r", " ").replace("\n", " ")
    return text if limit is None or len(text) <= limit else text[: limit - 1] + "…"


def to_csv(names: list[str], rows: list[dict]) -> str:
    """UTF-8 with a BOM (Excel shows Polish letters correctly). Cells that a spreadsheet could run as a formula
    (leading = + - @) get a leading apostrophe, so scraped text can't execute anything when opened."""
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\r\n")
    writer.writerow(names)
    for row in rows:
        cells = []
        for n in names:
            text = _cell(row.get(n))
            cells.append("'" + text if text.startswith(CSV_DANGEROUS_START) else text)
        writer.writerow(cells)
    return "﻿" + out.getvalue()


def summary(names: list[str], rows: list[dict], *, pages: int, url: str, fill: dict[str, float], reused: bool,
            more_pages: bool, recipe_note: str = "", stopped: str | None = None, preview_rows: int = 15,
            details_note: str = "") -> str:
    """What the planner and the operator read: counts, coverage, and the first rows as a table."""
    lines = [f"Scraped {len(rows)} items from {pages} page{'s' if pages != 1 else ''} starting at {url}.",
             "Fields: " + ", ".join(names) + ".",
             "Coverage: " + ", ".join(f"{n} {round(v * 100)}%" for n, v in fill.items()) + "."]
    if reused:
        lines.append("Used the saved recipe (no AI cost for reading the pages).")
    elif recipe_note:
        lines.append(f"Recipe: {recipe_note}")
    if details_note:
        lines.append(details_note)
    if stopped:
        lines.append(f"Stopped early: {stopped}. What was collected before that is kept with this step.")
    elif more_pages:
        lines.append("Stopped at the page/item limit: the site has more pages. The full data is saved with this step.")
    if rows:
        lines += ["", "| " + " | ".join(names) + " |", "|" + "---|" * len(names)]
        for row in rows[:preview_rows]:
            lines.append("| " + " | ".join(_cell(row.get(n), 60).replace("|", "/") for n in names) + " |")
        if len(rows) > preview_rows:
            lines.append(f"…and {len(rows) - preview_rows} more rows. The table is complete: every row has the same fields "
                         "(see Coverage) and is saved with this step for the operator to download. Don't re-read these pages.")
    return "\n".join(lines)
