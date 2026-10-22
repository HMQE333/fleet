"""Safe, polite fetching: the fetch worker must not be steerable into this machine or the local network."""

from urllib.parse import urlsplit

import pytest

from fleet import webfetch
from fleet.config import settings
from fleet.executors import Blocked, fetch
from fleet.fastbrowser import netguard

pytestmark = pytest.mark.no_db


@pytest.fixture(autouse=True)
def fast_pacing(monkeypatch):
    monkeypatch.setattr(settings(), "fetch_min_delay_seconds", 0)


@pytest.fixture
def local_test_site_is_fine(monkeypatch):
    """Let the test's own server (127.0.0.1) through the guard; every other address still gets the real check."""
    real = netguard.check_url_resolved

    async def check(url):
        return None if urlsplit(url).hostname == "127.0.0.1" else await real(url)

    monkeypatch.setattr(netguard, "check_url_resolved", check)


@pytest.mark.parametrize("url", [
    "http://127.0.0.1:8710/api/health",  # Fleet's own API (this was readable before the fix)
    "http://localhost:8710/api/site-accounts",
    "http://local.adspower.net:50325/api/v1/user/list",
    "http://192.168.1.1/admin", "http://169.254.169.254/latest/meta-data/", "http://[::1]/",
])
async def test_the_fetch_worker_refuses_local_and_private_addresses(url):
    with pytest.raises(Blocked, match="public web pages"):
        await fetch.fetch_markdown(url)


async def test_the_allowlist_matches_whole_domains(monkeypatch):
    async def public(url):
        return None

    monkeypatch.setattr(netguard, "check_url_resolved", public)
    await webfetch._check_target("https://shop.example.com/x", ["example.com"])
    await webfetch._check_target("https://example.com/", ["example.com"])
    with pytest.raises(Blocked, match="allowed domains"):
        await webfetch._check_target("https://evil-example.com/", ["example.com"])  # an "ends with" check let this pass


async def test_a_normal_page_is_read_and_menus_are_dropped(site, local_test_site_is_fine):
    page = await webfetch.get(f"{site}/shop.html")
    assert page.status == 200 and "Test shop" in page.html
    md, _ = await fetch.fetch_markdown(f"{site}/shop.html")
    assert "Test shop" in md


async def test_robots_txt_is_honoured(site, local_test_site_is_fine):
    with pytest.raises(Blocked, match="robots.txt"):
        await webfetch.get(f"{site}/private/secret.html")


async def test_the_final_url_after_redirects_is_checked_too(site, monkeypatch, local_test_site_is_fine):
    """If a fetch ends up somewhere the guard wouldn't allow, the content is thrown away."""
    real_get = webfetch.AsyncFetcher.get

    class Redirected:
        def __init__(self, resp):
            self.status, self.body, self.html_content = resp.status, resp.body, resp.html_content
            self.url = "http://169.254.169.254/latest/meta-data/"

    async def fake_get(url, **kw):
        resp = await real_get(url, **kw)
        return resp if url.endswith("robots.txt") else Redirected(resp)

    monkeypatch.setattr(webfetch.AsyncFetcher, "get", staticmethod(fake_get))
    with pytest.raises(Blocked, match="public web pages"):
        await webfetch.get(f"{site}/shop.html")


def test_bot_check_pages_are_recognised():
    assert webfetch.looks_like_bot_check("Just a moment... Checking your browser")
    assert not webfetch.looks_like_bot_check("Raspberry Pi 5 8GB - 399 zl")
