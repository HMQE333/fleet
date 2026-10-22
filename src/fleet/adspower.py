"""AdsPower Local API client.

The Local API only listens on the machine running AdsPower and allows roughly
one request per second, so every call goes through one lock that spaces them
out. Once a browser is open, controlling it over CDP is not rate limited.
"""

import asyncio
import logging
import time
from dataclasses import dataclass

import httpx

from fleet import vault
from fleet.config import settings

log = logging.getLogger("fleet.adspower")

MIN_INTERVAL = 1.1


class AdsPowerError(RuntimeError):
    pass


@dataclass
class OpenBrowser:
    adspower_id: str
    cdp_url: str  # ws://127.0.0.1:<port>/devtools/browser/<id>
    debug_port: str


class AdsPower:
    def __init__(self, base_url: str | None = None, api_key: str | None = None):
        self.base_url = (base_url or settings().adspower_url).rstrip("/")
        self._api_key = api_key  # None = read ADSPOWER_API_KEY from the vault on each call
        self._client = httpx.AsyncClient(base_url=self.base_url, timeout=90)
        self._lock = asyncio.Lock()
        self._last_call = 0.0

    async def _headers(self) -> dict:
        key = self._api_key if self._api_key is not None else await vault.get("ADSPOWER_API_KEY")
        return {"Authorization": f"Bearer {key}"} if key else {}

    async def _call(self, method: str, path: str, *, params: dict | None = None, json: dict | None = None) -> dict:
        headers = await self._headers()
        async with self._lock:
            wait = MIN_INTERVAL - (time.monotonic() - self._last_call)
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                resp = await self._client.request(method, path, params=params, json=json, headers=headers)
            except httpx.TransportError as e:
                raise AdsPowerError(f"AdsPower is not reachable at {self.base_url} — is the app running with the Local API on? ({e})") from e
            finally:
                self._last_call = time.monotonic()
        body = resp.json()
        if body.get("code") != 0:
            raise AdsPowerError(f"AdsPower {path}: {body.get('msg')}")
        return body.get("data") or {}

    async def status(self) -> bool:
        try:
            resp = await self._client.get("/status", timeout=5)
            return resp.status_code == 200 and resp.json().get("code") == 0
        except (httpx.HTTPError, ValueError):
            return False

    async def list_profiles(self) -> list[dict]:
        out, page = [], 1
        while True:
            data = await self._call("GET", "/api/v1/user/list", params={"page": page, "page_size": 100})
            batch = data.get("list", [])
            out.extend(batch)
            if len(batch) < 100:
                return out
            page += 1

    async def start(self, adspower_id: str, *, headless: bool = False) -> OpenBrowser:
        """Open the profile's browser, or return the connection info if it's already open."""
        active = await self._call("GET", "/api/v1/browser/active", params={"user_id": adspower_id})
        if active.get("status") == "Active" and active.get("ws", {}).get("puppeteer"):
            data = active
        else:
            params = {"user_id": adspower_id, "open_tabs": 1, "ip_tab": 0}
            if headless:
                params["headless"] = 1
            data = await self._call("GET", "/api/v1/browser/start", params=params)
        ws = data.get("ws", {}).get("puppeteer")
        if not ws:
            raise AdsPowerError(f"AdsPower started {adspower_id} but returned no CDP endpoint")
        return OpenBrowser(adspower_id=adspower_id, cdp_url=ws, debug_port=str(data.get("debug_port", "")))

    async def stop(self, adspower_id: str) -> None:
        await self._call("GET", "/api/v1/browser/stop", params={"user_id": adspower_id})

    async def aclose(self) -> None:
        await self._client.aclose()


_instance: AdsPower | None = None


def client() -> AdsPower:
    global _instance
    if _instance is None:
        _instance = AdsPower()
    return _instance
