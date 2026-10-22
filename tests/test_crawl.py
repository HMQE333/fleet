"""Crawling a site section: which links are followed, the limits, resuming, and the table that comes out.

The rules under test (RULES.md §6): only the start page's own site and folder, never files or nofollow links, robots.txt
honoured, a hard cap on pages and depth, a trap guard, stop after repeated refusals, no model and no cost.
"""

import asyncio
import functools
import http.server
import pathlib
import threading
from urllib.parse import urlsplit

import pytest

from fleet import automations, crawl, db, llm, scrape_store, webfetch
from fleet.config import settings
from fleet.executors import Blocked, RunContext
from fleet.executors import crawl as crawl_worker
from fleet.fastbrowser import netguard

from .conftest import finished, new_task, running_fleet, sub, subtasks, wait_for

BASE = "https://www.docs.test"


# -- the pure rules --------------------------------------------------------------------------------------------


@pytest.mark.no_db
@pytest.mark.parametrize("raw,expected", [
    ("https://WWW.Docs.Test:443/docs//a/index.html?utm_source=x&b=2&a=1#top", "https://www.docs.test/docs/a/?a=1&b=2"),
    ("http://docs.test:80/", "http://docs.test/"),
    ("http://docs.test:8080/a/", "http://docs.test:8080/a/"),
    ("https://docs.test/a?fbclid=9&ref=nav&page=2", "https://docs.test/a?page=2"),
    ("https://docs.test/a/index.html", "https://docs.test/a/"),
    ("https://docs.test/a?" + "q=" + "z" * 200, None),
    ("ftp://docs.test/a", None), ("mailto:a@b.c", None), ("//docs.test/a", None), ("", None),
])
def test_one_canonical_address_per_page(raw, expected):
    assert crawl.normalize(raw) == expected


@pytest.mark.no_db
def test_the_section_is_the_start_urls_own_folder():
    assert crawl.section_prefix(f"{BASE}/docs/") == "/docs"
    assert crawl.section_prefix(f"{BASE}/docs/intro.html") == "/docs"
    assert crawl.section_prefix(f"{BASE}/blog") == "/blog"
    assert crawl.section_prefix(BASE) == "/" and crawl.section_prefix(f"{BASE}/") == "/"
    scope = crawl.Scope.of(f"{BASE}/docs/", 3)
    inside = ["https://docs.test/docs", "https://www.docs.test/docs/a", "https://docs.test/docs/a/b/c.html?x=1"]
    outside = ["https://docs.test/docs-old/a", "https://docs.test/doc/a", "https://docs.test/blog/a", "https://other.test/docs/a",
               "https://docs.test/docs/manual.pdf", "https://docs.test/docs/logo.PNG", "https://docs.test/docs/feed.xml"]
    assert all(scope.contains(u) for u in inside) and not any(scope.contains(u) for u in outside)
    assert crawl.Scope.of(BASE, 2).contains("https://docs.test/anything/at/all")  # the whole site


@pytest.mark.no_db
def test_links_are_resolved_cleaned_deduplicated_and_nofollow_is_skipped():
    html = """<a href="/docs/a.html#top">a</a> <a href="a.html">same</a> <a href="javascript:void(0)">js</a>
      <a href="mailto:x@y.z">mail</a> <a href="#x">frag</a> <a href="https://other.test/x">off</a> <a href="/docs/b/?utm_medium=e">b</a>
      <a href="/docs/secret.html" rel="nofollow noopener">secret</a> <a href="">empty</a> <a>no href</a>"""
    assert crawl.extract_links(html, "https://docs.test/docs/index.html") == [
        "https://docs.test/docs/a.html", "https://other.test/x", "https://docs.test/docs/b/"]
    assert crawl.extract_links("", "https://docs.test/") == [] and crawl.extract_links("<<<", "https://docs.test/") == []


@pytest.mark.no_db
def test_a_calendar_that_makes_endless_variants_is_cut_off():
    guard = crawl.TrapGuard()
    allowed = [guard.allow(f"https://docs.test/cal?m={i}") for i in range(30)]
    assert sum(allowed) == crawl.MAX_QUERY_VARIANTS_PER_PATH
    assert guard.allow("https://docs.test/plain") and guard.allow("https://docs.test/other?x=1")


@pytest.mark.no_db
def test_titles_fall_back_to_the_first_heading():
    assert crawl.page_title("<html><head><title> My  page </title></head></html>") == "My page"
    assert crawl.page_title("<body><h1>Heading</h1></body>") == "Heading"
    assert crawl.page_title("<p>nothing</p>") == ""


# -- the worker over a small site ---------------------------------------------------------------------------------


def page(title: str, body: str, links: list[str] = ()) -> str:
    anchors = " ".join(f'<a href="{href}">{href}</a>' for href in links)
    return f"<html><head><title>{title}</title></head><body><nav>Menu</nav><h1>{title}</h1><p>{body}</p>{anchors}</body></html>"


SITE = {
    "docs/index.html": page("Docs home", "Welcome to the docs. " * 5, [
        "intro.html", "guide/", "../blog/post1.html", "https://other.example.net/x", "https://other.example.net/docs/offsite.html", "manual.pdf", "#top", "mailto:a@b.c",
        "private/x.html", "missing.html"]) + '<a href="secret.html" rel="nofollow">secret</a>',
    "docs/intro.html": page("Introduction", "Start here.", ["./", "guide/setup.html", "intro.html?utm_source=x"]),
    "docs/guide/index.html": page("Guide", "The guide.", ["setup.html"]),
    "docs/guide/setup.html": page("Setup", "Install it.", ["deep/a.html"]),
    "docs/guide/deep/a.html": page("Deep A", "Three clicks down.", ["b.html"]),
    "docs/guide/deep/b.html": page("Deep B", "Four clicks down.", []),
    "docs/secret.html": page("Secret", "nofollow target"),
    "docs/private/x.html": page("Private", "robots.txt says no"),
    "blog/post1.html": page("Blog", "outside the section"),
    "robots.txt": "User-agent: *\nDisallow: /docs/private/\n",
}


class Handler(http.server.SimpleHTTPRequestHandler):
    """Serves a folder's index.html at both /docs and /docs/ (the stock handler redirects, and Fleet refuses redirects
    to local addresses, as it should)."""

    def do_GET(self):
        path, _, query = self.path.partition("?")
        if (pathlib.Path(self.directory) / path.lstrip("/")).is_dir():
            self.path = path.rstrip("/") + "/index.html" + (f"?{query}" if query else "")
        super().do_GET()


def serve(root, files: dict[str, str]):
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    handler = functools.partial(Handler, directory=str(root))
    handler.log_message = lambda *a: None
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


@pytest.fixture
def site_dir(fake, tmp_path, monkeypatch):
    server = serve(tmp_path / "site", SITE)
    state = {"base": f"http://127.0.0.1:{server.server_address[1]}", "root": tmp_path / "site", "fetched": []}

    async def public(url):
        return None

    async def no_model(**kw):
        raise AssertionError("a crawl must not call a model")

    real_get = webfetch.get

    async def recording_get(url, **kw):
        state["fetched"].append(url)
        return await real_get(url, **kw)

    monkeypatch.setattr(netguard, "check_url_resolved", public)
    monkeypatch.setattr(llm, "structured", no_model)
    monkeypatch.setattr(llm, "complete", no_model)
    monkeypatch.setattr(webfetch, "get", recording_get)
    monkeypatch.setattr(settings(), "fetch_min_delay_seconds", 0)
    yield state
    server.shutdown()


async def _crawl(fake, site_dir, path="docs/"):
    fake.plan.subtasks = [sub("c1", kind="crawl", url=f"{site_dir['base']}/{path}")]
    task = await new_task()
    async with running_fleet():
        await wait_for(lambda: finished(task["id"]), timeout=90)
    return task, (await subtasks(task["id"]))["c1"]


def _paths(items) -> set[str]:
    return {urlsplit(i["url"]).path for i in items}


async def test_a_section_is_crawled_within_its_folder_depth_and_robots(fake, site_dir):
    task, c1 = await _crawl(fake, site_dir)
    assert c1["status"] == "done", c1["error"]
    stored = await scrape_store.get(c1["id"])
    assert stored["fields"] == ["url", "title", "depth", "words", "text"]
    assert _paths(stored["items"]) == {"/docs/", "/docs/intro.html", "/docs/guide/", "/docs/guide/setup.html", "/docs/guide/deep/a.html"}
    depth = {urlsplit(i["url"]).path: i["depth"] for i in stored["items"]}
    assert depth == {"/docs/": 0, "/docs/intro.html": 1, "/docs/guide/": 1, "/docs/guide/setup.html": 2, "/docs/guide/deep/a.html": 3}
    home = next(i for i in stored["items"] if i["url"].endswith("/docs/"))
    assert home["title"] == "Docs home" and home["words"] > 10 and "Welcome to the docs." in home["text"]
    assert "Menu" not in home["text"]  # navigation chrome is trimmed
    fetched_paths = {urlsplit(u).path for u in site_dir["fetched"]}
    for never in ("/blog/post1.html", "/docs/manual.pdf", "/docs/secret.html", "/docs/guide/deep/b.html"):
        assert never not in fetched_paths, never  # other section, a file, a nofollow link, deeper than 3 clicks
    assert not any("other.example.net" in u for u in site_dir["fetched"])
    assert "/docs/private/x.html" not in {urlsplit(i["url"]).path for i in stored["items"]}  # robots.txt says no
    assert stored["more_pages"] is False and stored["recipe"]["crawl"]["done"] is True and "frontier" not in stored["recipe"]["crawl"]
    assert "Crawled 5 pages of 127.0.0.1/docs" in c1["result"] and "| /docs/intro.html | Introduction |" in c1["result"]
    assert "2 pages were skipped" in c1["result"]  # missing.html answered 404, private/ is disallowed by robots.txt
    assert float(c1["expected_cost"]) == 0.0 and float(c1["cost_usd"] or 0) == 0.0


async def test_each_page_is_read_once_however_it_was_linked(fake, site_dir):
    await _crawl(fake, site_dir)
    paths = [urlsplit(u).path for u in site_dir["fetched"] if "robots" not in u]
    assert len(paths) == len(set(paths))  # intro.html?utm_source=x and ./ are the pages already seen


async def test_the_page_limit_is_hard_and_reported(fake, site_dir, monkeypatch):
    monkeypatch.setattr(settings(), "crawl_max_pages", 2)
    _, c1 = await _crawl(fake, site_dir)
    stored = await scrape_store.get(c1["id"])
    assert len(stored["items"]) == 2 and stored["more_pages"] is True
    assert "Stopped at the page limit" in c1["result"]


async def test_the_depth_limit_keeps_a_crawl_close_to_where_it_started(fake, site_dir, monkeypatch):
    monkeypatch.setattr(settings(), "crawl_max_depth", 1)
    _, c1 = await _crawl(fake, site_dir)
    assert _paths((await scrape_store.get(c1["id"]))["items"]) == {"/docs/", "/docs/intro.html", "/docs/guide/"}


async def test_a_calendar_trap_is_cut_off(fake, site_dir):
    site_dir["root"].joinpath("cal").mkdir()
    (site_dir["root"] / "cal" / "index.html").write_text(
        page("Calendar", "Pick a month.", [f"?month={m}" for m in range(1, 40)]), encoding="utf-8")
    _, c1 = await _crawl(fake, site_dir, "cal/")
    stored = await scrape_store.get(c1["id"])
    assert c1["status"] == "done" and len(stored["items"]) <= 1 + crawl.MAX_QUERY_VARIANTS_PER_PATH


async def test_repeated_refusals_stop_the_crawl_politely(fake, site_dir, monkeypatch):
    real = webfetch.get

    async def refusing(url, **kw):
        if urlsplit(url).path != "/docs/":
            site_dir["fetched"].append(url)
            raise Blocked(f"{url} answered HTTP 429: likely needs a real browser")
        return await real(url, **kw)

    monkeypatch.setattr(webfetch, "get", refusing)
    _, c1 = await _crawl(fake, site_dir)
    assert c1["status"] == "done", c1["error"]
    assert len((await scrape_store.get(c1["id"]))["items"]) == 1 and "the site keeps refusing pages" in c1["result"]
    assert len([u for u in site_dir["fetched"] if "/docs/" in u]) == 4  # the start page, then three refusals


async def test_a_crawl_that_reads_nothing_fails_clearly(fake, site_dir):
    _, c1 = await _crawl(fake, site_dir, "nothing-here/")
    assert c1["status"] == "failed" and "No page could be read" in c1["error"]


async def test_a_crawl_step_needs_a_start_url(fake, site_dir):
    fake.plan.subtasks = [sub("c1", kind="crawl", url=None)]
    task = await new_task()
    async with running_fleet():
        await wait_for(lambda: finished(task["id"]), timeout=40)
    assert "Crawl subtask without a URL" in (await subtasks(task["id"]))["c1"]["error"]


async def test_an_interrupted_crawl_resumes_instead_of_starting_over(site_dir, monkeypatch):
    monkeypatch.setattr(crawl_worker, "SAVE_EVERY", 2)
    p = await db.pool()
    task = await p.fetchrow("insert into tasks (prompt, status) values ('crawl', 'running') returning *")
    step = await p.fetchrow(
        "insert into subtasks (task_id, round, key, title, kind, instructions, start_url, status) "
        "values ($1, 1, 'c1', 'Crawl', 'crawl', 'x', $2, 'running') returning *", task["id"], f"{site_dir['base']}/docs/")
    ctx = RunContext(task=dict(task), subtask=dict(step), dependency_results={}, worker_id="t")
    real = webfetch.get
    count = {"n": 0}

    async def dies_on_the_fourth_page(url, **kw):
        if "robots" not in url:
            count["n"] += 1
            if count["n"] == 4:
                raise asyncio.CancelledError  # the worker is killed mid-crawl
        return await real(url, **kw)

    monkeypatch.setattr(webfetch, "get", dies_on_the_fourth_page)
    with pytest.raises(asyncio.CancelledError):
        await crawl_worker.run(ctx)
    partial = await scrape_store.get(step["id"])
    assert len(partial["items"]) == 2 and partial["recipe"]["crawl"]["done"] is False and partial["recipe"]["crawl"]["frontier"]

    monkeypatch.setattr(webfetch, "get", real)
    site_dir["fetched"].clear()
    result = await crawl_worker.run(ctx)  # the same step, claimed again
    done = await scrape_store.get(step["id"])
    assert _paths(done["items"]) == {"/docs/", "/docs/intro.html", "/docs/guide/", "/docs/guide/setup.html", "/docs/guide/deep/a.html"}
    assert len(done["items"]) == 5 and done["recipe"]["crawl"]["done"] is True
    already = {i["url"] for i in partial["items"]}
    assert not already & set(site_dir["fetched"])  # the two saved pages were not read again
    assert not any("sitemap" in u for u in site_dir["fetched"])  # a resumed crawl keeps the pages it already queued
    assert result.steps == 5
    assert await p.fetchval("select count(*) from events where message like 'Resuming the crawl: 2 pages already collected%'") == 1


async def test_a_saved_crawl_reports_new_pages_and_edited_text_each_run(fake, site_dir):
    task, _ = await _crawl(fake, site_dir)
    a = await automations.create_from_task(task["id"], "Docs watch", None, "UTC")
    assert a["steps"][0]["kind"] == "crawl"
    (site_dir["root"] / "docs" / "intro.html").write_text(
        page("Introduction", "Start here, now with a new paragraph.", ["./", "guide/setup.html", "news.html"]), encoding="utf-8")
    (site_dir["root"] / "docs" / "news.html").write_text(page("News", "A brand new page."), encoding="utf-8")
    run = await automations.start_run(a["id"])
    async with running_fleet():
        await wait_for(lambda: finished(run["id"]), timeout=90)
    again = (await subtasks(run["id"]))["c1"]
    assert again["status"] == "done", again["error"]
    changes = (await scrape_store.get(again["id"]))["changes"]
    assert changes["key"] == "url" and changes["counts"]["added"] == 1 and changes["added"][0]["title"] == "News"
    edited = next(c for c in changes["changed"] if c["key"].endswith("/docs/intro.html"))
    assert "text" in edited["changes"] and "now with a new paragraph" in edited["changes"]["text"]["to"]
    assert again["result"].startswith("Changes since the previous run") and "+ News" in again["result"]
    assert [str(u["id"]) for u in await scrape_store.updates()] == [str(again["id"])]  # and it shows in the Inbox updates


# -- sitemaps ---------------------------------------------------------------------------------------------------------


def _urlset(*locs: str) -> str:
    return '<?xml version="1.0" encoding="UTF-8"?><urlset>' + "".join(f"<url><loc>{u}</loc></url>" for u in locs) + "</urlset>"


def _orphans(base: str) -> dict[str, str]:
    """Pages nothing links to, listed only by the sitemap."""
    return {"docs/orphan1.html": page("Orphan one", "Only the sitemap knows me."),
            "docs/orphan2.html": page("Orphan two", "Me too.", ["intro.html"])}


@pytest.mark.no_db
def test_a_sitemap_is_read_with_a_pattern_not_an_xml_parser():
    text = ('<!DOCTYPE x [<!ENTITY e SYSTEM "file:///etc/passwd">]><urlset><url><loc>https://a.test/x?a=1&amp;b=2</loc></url>'
            "<url><LOC> https://a.test/y </LOC></url><url><loc></loc></url></urlset>")
    assert crawl.sitemap_locs(text) == ["https://a.test/x?a=1&b=2", "https://a.test/y"]  # the entity is never expanded
    assert crawl.is_sitemap_index("<sitemapindex><sitemap><loc>https://a.test/s.xml</loc></sitemap></sitemapindex>")
    assert not crawl.is_sitemap_index(text) and crawl.sitemap_locs("") == []


async def test_pages_only_the_sitemap_lists_are_found_inside_the_section(fake, site_dir):
    base = site_dir["base"]
    for name, content in _orphans(base).items():
        (site_dir["root"] / name).write_text(content, encoding="utf-8")
    (site_dir["root"] / "sitemap.xml").write_text(_urlset(
        f"{base}/docs/orphan1.html", f"{base}/docs/orphan2.html", f"{base}/docs/intro.html", f"{base}/blog/post1.html",
        "https://other.example.net/docs/offsite.html", f"{base}/docs/archive.zip"), encoding="utf-8")
    _, c1 = await _crawl(fake, site_dir)
    assert c1["status"] == "done", c1["error"]
    stored = await scrape_store.get(c1["id"])
    paths = _paths(stored["items"])
    assert {"/docs/orphan1.html", "/docs/orphan2.html"} <= paths and len(paths) == 7
    assert "/blog/post1.html" not in paths and "/docs/archive.zip" not in paths  # another section, a file
    assert not any("other.example.net" in u for u in site_dir["fetched"])
    depth = {urlsplit(i["url"]).path: i["depth"] for i in stored["items"]}
    assert depth["/docs/orphan1.html"] == 1 and depth["/docs/intro.html"] == 1  # listed pages count as one click away
    assert "The site's sitemap lists 2 more pages in this section" in c1["result"]  # intro.html was already linked
    paths_fetched = [urlsplit(u).path for u in site_dir["fetched"] if "robots" not in u]
    assert len(paths_fetched) == len(set(paths_fetched))  # still every page once


async def test_a_sitemap_index_is_followed_a_few_files_deep_on_the_same_site_only(fake, site_dir):
    base = site_dir["base"]
    for name, content in _orphans(base).items():
        (site_dir["root"] / name).write_text(content, encoding="utf-8")
    (site_dir["root"] / "sitemap.xml").write_text(
        "<sitemapindex><sitemap><loc>" + f"{base}/sitemap-docs.xml" + "</loc></sitemap><sitemap><loc>https://other.example.net/s.xml</loc></sitemap></sitemapindex>",
        encoding="utf-8")
    (site_dir["root"] / "sitemap-docs.xml").write_text(_urlset(f"{base}/docs/orphan1.html"), encoding="utf-8")
    _, c1 = await _crawl(fake, site_dir)
    assert "/docs/orphan1.html" in _paths((await scrape_store.get(c1["id"]))["items"])
    assert not any("other.example.net" in u for u in site_dir["fetched"])


async def test_a_sitemap_robots_txt_disallows_is_not_read(fake, site_dir):
    base = site_dir["base"]
    for name, content in _orphans(base).items():
        (site_dir["root"] / name).write_text(content, encoding="utf-8")
    (site_dir["root"] / "sitemap.xml").write_text(_urlset(f"{base}/docs/orphan1.html"), encoding="utf-8")
    (site_dir["root"] / "robots.txt").write_text("User-agent: *\nDisallow: /docs/private/\nDisallow: /sitemap.xml\n", encoding="utf-8")
    _, c1 = await _crawl(fake, site_dir)
    assert c1["status"] == "done" and "/docs/orphan1.html" not in _paths((await scrape_store.get(c1["id"]))["items"])
    assert "sitemap lists" not in c1["result"]


async def test_the_number_of_pages_taken_from_sitemaps_is_capped(fake, site_dir, monkeypatch):
    monkeypatch.setattr(crawl, "MAX_SITEMAP_URLS", 3)
    base = site_dir["base"]
    for i in range(20):
        (site_dir["root"] / "docs" / f"extra{i}.html").write_text(page(f"Extra {i}", "x"), encoding="utf-8")
    (site_dir["root"] / "sitemap.xml").write_text(_urlset(*[f"{base}/docs/extra{i}.html" for i in range(20)]), encoding="utf-8")
    _, c1 = await _crawl(fake, site_dir)
    extras = [i for i in (await scrape_store.get(c1["id"]))["items"] if "/extra" in i["url"]]
    assert len(extras) == 3 and "sitemap lists 3 more pages" in c1["result"]


async def test_a_one_page_crawl_never_asks_for_the_sitemap(fake, site_dir, monkeypatch):
    monkeypatch.setattr(settings(), "crawl_max_depth", 0)
    _, c1 = await _crawl(fake, site_dir)
    assert not any("sitemap" in u for u in site_dir["fetched"]) and len((await scrape_store.get(c1["id"]))["items"]) == 1
