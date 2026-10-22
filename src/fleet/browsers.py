"""One way to open a profile's browser, whatever runs it: an AdsPower profile through the Local API, or a local Chrome /
Edge profile Fleet starts itself (its own user data directory under data/profiles, headless or visible).

The executors only ever see a CDP address. Local profiles need no AdsPower, no paid plan and no extra program; they are
the operator's own browser profiles and the same rules apply (RULES.md §1, §5): one real identity per profile, one
worker at a time, logins done by the operator in a visible window.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import socket
import subprocess
import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from fleet import adspower
from fleet.config import settings

log = logging.getLogger("fleet.browsers")

LOCAL = "local"
ADSPOWER = "adspower"

CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    "/usr/bin/google-chrome", "/usr/bin/google-chrome-stable", "/usr/bin/chromium", "/usr/bin/chromium-browser",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
]


class BrowserError(RuntimeError):
    """The profile's browser couldn't be opened. Written for the operator."""


@dataclass
class Opened:
    cdp_url: str  # ws://127.0.0.1:<port>/devtools/browser/<id>
    kind: str
    debug_port: str = ""


def kind_of(profile: dict) -> str:
    return profile.get("kind") or ADSPOWER


def executable() -> str | None:
    """The browser Fleet starts for local profiles: FLEET_BROWSER_EXE, else the first Chrome or Edge it finds."""
    configured = settings().browser_exe
    if configured:
        return configured if Path(configured).exists() else shutil.which(configured)
    for cand in CANDIDATES:
        if Path(cand).exists():
            return cand
    return shutil.which("google-chrome") or shutil.which("chromium") or shutil.which("msedge")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _data_dir(profile: dict) -> Path:
    """Always absolute: Chrome rejects a relative --user-data-dir and exits at once (code 21), and Fleet's data dir is
    relative by default ("data/")."""
    if profile.get("data_dir"):
        return Path(profile["data_dir"]).absolute()
    return (settings().data_dir / "profiles" / str(profile["id"])).absolute()


# One process per profile id in this worker; the lease (fleet/profiles.py) keeps two workers off the same profile.
_running: dict[str, tuple[subprocess.Popen, Opened]] = {}


def _alive(key: str) -> Opened | None:
    entry = _running.get(key)
    if entry is None:
        return None
    proc, opened = entry
    if proc.poll() is not None:
        _running.pop(key, None)
        return None
    return opened


async def _start_local(profile: dict, *, visible: bool = False) -> Opened:
    key = str(profile["id"])
    if opened := _alive(key):
        return opened
    exe = executable()
    if not exe:
        raise BrowserError("No Chrome or Edge found for local profiles. Install one, or set FLEET_BROWSER_EXE to the browser's path.")
    headless = bool(profile.get("headless", True)) and not visible
    data_dir = _data_dir(profile)
    data_dir.mkdir(parents=True, exist_ok=True)
    port = _free_port()
    args = [exe, f"--remote-debugging-port={port}", f"--user-data-dir={data_dir}", "--no-first-run",
            "--no-default-browser-check", "--disable-background-networking", "--disable-sync"]
    if headless:
        # A desktop-sized window: headless Chrome is 800x600 by default, so the fast browser (which is only ever shown
        # what the viewport holds) would see a quarter of an ordinary page.
        args += ["--headless=new", "--disable-gpu", "--window-size=1280,1024"]
    args.append("about:blank")
    creation = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if sys.platform == "win32" else {}
    try:
        proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **creation)
    except OSError as e:
        raise BrowserError(f"Couldn't start {exe}: {e}") from e
    deadline = asyncio.get_running_loop().time() + 30
    while asyncio.get_running_loop().time() < deadline:
        if proc.poll() is not None:
            raise BrowserError(f"The browser exited right after starting (code {proc.returncode}). Is another browser using {data_dir}?")
        try:
            info = await asyncio.to_thread(_version, port)
            opened = Opened(cdp_url=info["webSocketDebuggerUrl"], kind=LOCAL, debug_port=str(port))
            _running[key] = (proc, opened)
            log.info("local profile %s up (%s, port %s)", profile.get("name"), "headless" if headless else "visible", port)
            return opened
        except (OSError, ValueError, KeyError):
            await asyncio.sleep(0.2)
    proc.kill()
    raise BrowserError("The browser started but never answered on its debugging port (30 s).")


def _version(port: int) -> dict:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=1) as r:
        return json.load(r)


async def open(profile: dict, *, visible: bool = False) -> Opened:  # noqa: A001 - the module's verb
    """Open (or attach to) the profile's browser and return its CDP address. `visible` forces a window for a local
    profile that is normally headless (the operator wants to log in or finish a challenge by hand)."""
    if kind_of(profile) == LOCAL:
        return await _start_local(profile, visible=visible)
    try:
        opened = await adspower.client().start(profile["adspower_id"])
    except adspower.AdsPowerError as e:
        raise BrowserError(str(e)) from e
    return Opened(cdp_url=opened.cdp_url, kind=ADSPOWER, debug_port=opened.debug_port)


async def close(profile: dict) -> None:
    """Close the profile's browser. Never raises: a browser that won't close is logged, not a failed step."""
    if kind_of(profile) == LOCAL:
        entry = _running.pop(str(profile["id"]), None)
        if entry is None:
            return
        proc, _ = entry
        try:
            proc.terminate()
            await asyncio.to_thread(proc.wait, 10)
        except subprocess.TimeoutExpired:
            proc.kill()
        except OSError as e:
            log.warning("could not close local profile %s: %s", profile.get("name"), e)
        return
    try:
        await adspower.client().stop(profile["adspower_id"])
    except adspower.AdsPowerError as e:
        log.warning("could not close profile %s: %s", profile.get("name"), e)


def window_name(profile: dict | None) -> str:
    """How to tell the operator where to look: "the AdsPower window for profile X" or "the Chrome window for profile X"."""
    name = (profile or {}).get("name")
    if profile and kind_of(profile) == LOCAL:
        return f"the browser window for profile “{name}”" + (" (it runs headless: open it from the Profiles page first)"
                                                            if profile.get("headless", True) else "")
    return f"the AdsPower window for profile “{name}”"


def close_all() -> None:
    """At process exit: no local browser outlives the worker that started it."""
    for key, (proc, _) in list(_running.items()):
        try:
            proc.terminate()
        except OSError:
            pass
        _running.pop(key, None)


if os.environ.get("FLEET_NO_ATEXIT") != "1":
    import atexit

    atexit.register(close_all)
