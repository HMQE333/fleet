"""Minimal async Chrome DevTools Protocol client: one websocket, flattened target sessions.

Fleet attaches to the AdsPower profile's own browser endpoint, so the agent's tab shares the
profile's cookies, fingerprint and proxy. The engine polls page state; the few events it needs
(targets opened by its own tab) go to listeners registered with `on`.
"""

import asyncio
import itertools
import json
import logging

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

log = logging.getLogger("fleet.fastbrowser.cdp")


class CDPError(RuntimeError):
    pass


class CDP:
    def __init__(self, ws_url: str):
        self.ws_url = ws_url
        self._ws = None
        self._reader: asyncio.Task | None = None
        self._ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future] = {}
        self._listeners: dict[str, list] = {}

    async def __aenter__(self) -> "CDP":
        try:
            self._ws = await connect(self.ws_url, max_size=None, ping_interval=None, open_timeout=15)
        except OSError as e:
            raise CDPError(f"Can't connect to the browser at {self.ws_url}: {e}") from e
        self._reader = asyncio.create_task(self._read())
        return self

    async def __aexit__(self, *_exc) -> None:
        if self._reader:
            self._reader.cancel()
        if self._ws is not None:
            await self._ws.close()
        self._fail_pending("connection closed")

    async def _read(self) -> None:
        try:
            async for raw in self._ws:
                msg = json.loads(raw)
                if "method" in msg:
                    for listener in list(self._listeners.get(msg["method"], ())):
                        try:
                            listener(msg.get("params") or {})
                        except Exception:  # noqa: BLE001 - a listener's bug must not kill the reader
                            log.exception("CDP event listener failed for %s", msg["method"])
                    continue
                fut = self._pending.pop(msg.get("id"), None) if "id" in msg else None
                if fut is None or fut.done():
                    continue
                if "error" in msg:
                    fut.set_exception(CDPError(msg["error"].get("message", str(msg["error"]))))
                else:
                    fut.set_result(msg.get("result", {}))
        except ConnectionClosed:
            pass
        finally:
            self._fail_pending("the browser closed the connection")

    def _fail_pending(self, why: str) -> None:
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(CDPError(why))
        self._pending.clear()

    def on(self, method: str, listener) -> None:
        """Call `listener(params)` for every event of this method (browser- and session-level alike)."""
        self._listeners.setdefault(method, []).append(listener)

    def off(self, method: str, listener) -> None:
        if listener in self._listeners.get(method, ()):
            self._listeners[method].remove(listener)

    async def send(self, method: str, params: dict | None = None, *, session_id: str | None = None,
                   timeout: float = 30.0) -> dict:
        if self._ws is None:
            raise CDPError("not connected")
        msg_id = next(self._ids)
        fut = asyncio.get_running_loop().create_future()
        self._pending[msg_id] = fut
        msg: dict = {"id": msg_id, "method": method, "params": params or {}}
        if session_id:
            msg["sessionId"] = session_id
        try:
            await self._ws.send(json.dumps(msg))
        except ConnectionClosed as e:
            self._pending.pop(msg_id, None)
            raise CDPError("the browser closed the connection") from e
        try:
            return await asyncio.wait_for(fut, timeout)
        except TimeoutError as e:
            self._pending.pop(msg_id, None)
            raise CDPError(f"{method} timed out after {timeout:.0f}s") from e
