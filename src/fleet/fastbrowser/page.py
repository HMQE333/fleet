"""One agent tab in the profile's browser: observe, check freshness, act.

Async port of jev-ultrafast's browser.py (MIT, see NOTICE.md). Code-owned node IDs refer to
elements the page reader actually observed — never model-written selectors — and every input is
preceded by a freshness check and a hit test, so a decision about a page that has since changed
is refused instead of clicking the wrong thing.

Unlike upstream, no viewport override: an AdsPower profile's screen size is part of its fingerprint.

A click that opens a new tab doesn't lose the hands: every tab the engine's tab opens (Target.targetCreated with this
tab as the opener, which Chrome reports even for a noopener link) is remembered, and `opened_tab()` attaches to it so
the engine can observe and act there; the engine closes it when it comes back or when the step ends.
"""

import asyncio
import hashlib
import json
import sys
from pathlib import Path

from fleet.fastbrowser.cdp import CDP, CDPError

SELECT_ALL_MODIFIER = 4 if sys.platform == "darwin" else 2  # Cmd on macOS, Ctrl elsewhere
READ_STATE = Path(__file__).with_name("snapshot.js").read_text(encoding="utf-8")
MARKER = f"(() => {{ const state={READ_STATE}; return state?.marker ?? null; }})()"

SETTLE_JS = """(action => new Promise(resolve => {
  const field=window.__jevFast?.nodes.get(action.node), doc=field?.ownerDocument || document;
  const autocomplete=action.kind==='fill' && field?.getAttribute('role')==='combobox';
  let frames=0, stopped=false;
  const finish=()=>{stopped=true;resolve()};
  setTimeout(finish,autocomplete ? 200 : 50);
  const ready=()=>{
    if (stopped) return;
    const ids=(field?.getAttribute('aria-controls')||field?.getAttribute('aria-owns')||'')
      .split(/\\s+/).filter(Boolean);
    const roots=ids.length ? ids.map(id=>doc.getElementById(id)).filter(Boolean) : [doc];
    const options=roots.flatMap(root=>[...root.querySelectorAll('[role="option"]')]);
    if (++frames>=2 && (!autocomplete || options.some(e=>{
      const r=e.getBoundingClientRect();
      return r.width && r.height && r.bottom>0 && r.top<innerHeight &&
        e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true});
    }))) finish();
    else requestAnimationFrame(ready);
  };
  requestAnimationFrame(ready);
}))"""

TARGET_JS = """(action => {
  const e=window.__jevFast?.nodes.get(action.node);
  if (!e?.isConnected || e.matches(':disabled') || e.closest('[aria-disabled="true"],[inert]') ||
      !e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true})) return null;
  if (action.kind==='fill' && (e.readOnly || e.getAttribute('aria-readonly')==='true')) return null;
  // Frame-local geometry first (the element must be the thing under the point we aim at: the centre of the part
  // of it that is on screen, so a control cut by the fold is still usable — snapshot.js offers an action on the
  // same rule, with the same MIN_VISIBLE), then up through every enclosing same-origin frame: each must be on
  // screen and be what its parent shows at that point.
  const MIN_VISIBLE=4;
  const r=e.getBoundingClientRect();
  let doc=e.ownerDocument, win=doc.defaultView;
  const x0=Math.max(r.x,0), x1=Math.min(r.right,win.innerWidth);
  const y0=Math.max(r.y,0), y1=Math.min(r.bottom,win.innerHeight);
  if (!r.width || !r.height || x1-x0<Math.min(MIN_VISIBLE,r.width) || y1-y0<Math.min(MIN_VISIBLE,r.height))
    return null;
  let x=(x0+x1)/2, y=(y0+y1)/2;
  if (!e.contains(doc.elementFromPoint(x,y))) return null;
  while (win!==window) {
    const f=win.frameElement; if (!f) return null;
    const fr=f.getBoundingClientRect();
    x+=fr.x+f.clientLeft; y+=fr.y+f.clientTop; doc=f.ownerDocument; win=doc.defaultView;
    if (x<0 || y<0 || x>=win.innerWidth || y>=win.innerHeight || !f.contains(doc.elementFromPoint(x,y))) return null;
  }
  if (action.kind==='select') {
    if (e.tagName!=='SELECT' || ![...e.options].some(o=>o.value===action.value &&
        !o.disabled && !o.closest('optgroup[disabled]'))) return null;
    e.value=action.value;
    e.dispatchEvent(new Event('input',{bubbles:true}));
    e.dispatchEvent(new Event('change',{bubbles:true}));
  }
  return {x,y};
})"""


# The whole page's text (the reader only sees the viewport), with the text of every same-origin frame in it.
FULL_TEXT_JS = """(() => {
  const docs=window.__jevFast?.docs ? window.__jevFast.docs() : [document];
  return docs.map(d=>d.body ? d.body.innerText : '').join('\\n').slice(0, 40000);
})()"""


class StalePage(ValueError):
    """A decision no longer refers to the observed page."""


def fingerprint(state: dict) -> str:
    content = {k: state[k] for k in ("url", "text", "actions", "scroll")}
    return hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()


class Page:
    def __init__(self, cdp: CDP, target_id: str, session_id: str):
        self.cdp = cdp
        self.target_id = target_id
        self.session_id = session_id
        self.after_input: dict | None = None
        self.opened: list[str] = []  # ids of the tabs this tab opened, oldest first, not yet followed
        cdp.on("Target.targetCreated", self._on_target_created)
        cdp.on("Target.targetDestroyed", self._on_target_destroyed)

    def _on_target_created(self, params: dict) -> None:
        info = params.get("targetInfo") or {}
        if info.get("type") == "page" and info.get("openerId") == self.target_id:
            self.opened.append(info["targetId"])

    def _on_target_destroyed(self, params: dict) -> None:
        if params.get("targetId") in self.opened:
            self.opened.remove(params["targetId"])

    @classmethod
    async def open(cls, cdp: CDP, url: str, *, timeout: float = 20.0) -> "Page":
        await cdp.send("Target.setDiscoverTargets", {"discover": True})  # so tabs this tab opens are seen
        target = (await cdp.send("Target.createTarget", {"url": "about:blank"}))["targetId"]
        page = await cls.attach(cdp, target)
        try:
            await page.call("Page.navigate", url=url)
            await page.wait_ready(timeout)
        except Exception:
            await page.close()
            raise
        return page

    @classmethod
    async def attach(cls, cdp: CDP, target_id: str) -> "Page":
        session = (await cdp.send("Target.attachToTarget", {"targetId": target_id, "flatten": True}))["sessionId"]
        page = cls(cdp, target_id, session)
        try:
            # Keep rAF/menus rendering even if the window isn't focused.
            await page.call("Emulation.setFocusEmulationEnabled", enabled=True)
        except Exception:
            await page.close()
            raise
        return page

    async def opened_tab(self, *, timeout: float = 10.0) -> "Page | None":
        """The tab the last action opened, attached and loaded, or None. A click that opened several tabs is followed
        into the first; the others are closed (they are this tab's, nobody else will)."""
        if not self.opened:
            return None
        first, *rest = self.opened
        self.opened.clear()
        for extra in rest:
            try:
                await self.cdp.send("Target.closeTarget", {"targetId": extra}, timeout=5)
            except CDPError:
                pass
        try:
            page = await Page.attach(self.cdp, first)
        except CDPError:
            return None  # gone already (closed itself, or a download)
        # A new tab starts life on about:blank; its real address lands a moment later.
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            url = await page.url()
            if url and url != "about:blank":
                break
            await asyncio.sleep(0.05)
        await page.wait_ready(timeout)
        return page

    async def call(self, method: str, timeout: float = 30.0, **params) -> dict:
        return await self.cdp.send(method, params, session_id=self.session_id, timeout=timeout)

    async def evaluate(self, expression: str, *, await_promise: bool = False):
        response = await self.call("Runtime.evaluate", expression=expression, returnByValue=True,
                                   awaitPromise=await_promise)
        if response.get("exceptionDetails"):
            raise StalePage("Document changed during evaluation")
        return response.get("result", {}).get("value")

    async def wait_ready(self, timeout: float = 15.0) -> None:
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            try:
                if await self.evaluate("document.readyState") == "complete":
                    return
            except (StalePage, CDPError):
                pass
            await asyncio.sleep(0.05)

    async def url(self) -> str:
        try:
            return await self.evaluate("location.href") or ""
        except (StalePage, CDPError):
            return ""

    async def observe(self, screenshot: bool = True, settle_seconds: float = 12.0) -> dict:
        if self.after_input:
            action, self.after_input = self.after_input, None
            try:  # read-only settle wait after input (autocomplete lists, re-renders)
                await self.call("Runtime.evaluate", expression=f"{SETTLE_JS}({json.dumps(action)})",
                                awaitPromise=True, returnByValue=True, timeout=5)
            except CDPError:
                pass
        # A page that is mid-navigation has no body for a while (a real site: seconds). Real run: 10 tries of 50 ms gave up on
        # Wikipedia's first article load and handed a working task to the expensive agent.
        loop = asyncio.get_running_loop()
        deadline = loop.time() + settle_seconds
        delay = 0.05
        while True:
            try:
                info = await self.evaluate(READ_STATE)
                if info is None:
                    raise StalePage("Document is navigating")
                info["fingerprint"] = fingerprint(info)
                if screenshot:
                    shot = await self.call("Page.captureScreenshot", format="jpeg", quality=70)
                    info["screenshot"] = shot["data"]
                return info
            except StalePage:
                if loop.time() + delay > deadline:
                    raise
                await asyncio.sleep(delay)
                delay = min(delay * 2, 0.5)

    async def fresh(self, page: dict, action: dict | None = None) -> bool:
        try:
            if action is not None and action["kind"] in ("click", "select", "upload"):
                node = action["node"]
                if type(node) is not int:
                    return False
                current = await self.evaluate(
                    "(() => { const c=window.__jevFast; "
                    f"return c ? [c.pageKey(),c.guard(c.nodes.get({node}))] : null; }})()"
                )
                return current == [page["page_key"], page["guards"].get(str(node))]
            return await self.evaluate(MARKER) == page["marker"]
        except StalePage:
            return False

    async def act(self, action: dict, page: dict, text: str | None = None) -> None:
        if not await self.fresh(page, action):
            raise StalePage("Page changed since this decision. Observe again.")
        kind = action["kind"]
        if kind == "wait":
            await asyncio.sleep(0.4)
            return
        if kind == "scroll":
            await self.call("Input.dispatchMouseEvent", type="mouseWheel", x=page.get("w", 800) // 2,
                            y=page.get("h", 600) // 2, deltaX=0, deltaY=action["delta"])
            self.after_input = action
            return
        if type(action.get("node")) is not int:
            raise ValueError("Invalid observed node")
        try:
            target = await self.evaluate(f"{TARGET_JS}({json.dumps(action)})")
        except StalePage:
            if kind == "select":
                raise RuntimeError("Dropdown change was interrupted; observe before retrying.") from None
            raise
        if target is None:
            if kind == "select":
                raise RuntimeError("Dropdown change was not confirmed; observe before retrying.")
            raise StalePage("Target changed or is covered. Observe again.")
        if kind == "upload":
            await self.set_files(action["node"], [text or ""])
        elif kind != "select":
            x, y = target["x"], target["y"]
            for event in ("mousePressed", "mouseReleased"):
                await self.call("Input.dispatchMouseEvent", type=event, x=x, y=y, button="left", clickCount=1)
            if kind == "fill":
                await self.call("Input.dispatchKeyEvent", type="keyDown", key="a", code="KeyA",
                                modifiers=SELECT_ALL_MODIFIER, commands=["selectAll"])
                await self.call("Input.dispatchKeyEvent", type="keyUp", key="a", code="KeyA",
                                modifiers=SELECT_ALL_MODIFIER)
                await self.call("Input.insertText", text=text or "")
        self.after_input = action

    async def set_files(self, node: int, paths: list[str]) -> None:
        """Set a file input's files through the protocol (no native dialog): `node` is an observed, hit-tested file input."""
        handle = await self.call("Runtime.evaluate", expression=f"window.__jevFast.nodes.get({node})")
        object_id = handle.get("result", {}).get("objectId")
        if not object_id:
            raise StalePage("File input is gone. Observe again.")
        await self.call("DOM.setFileInputFiles", files=paths, objectId=object_id)
        await self.evaluate(f"window.__jevFast.nodes.get({node}).dispatchEvent(new Event('change', {{bubbles: true}}))")

    async def back(self) -> None:
        try:
            await self.evaluate("history.back()")
        except StalePage:
            pass
        await asyncio.sleep(0.3)
        await self.wait_ready(10)

    async def close(self) -> None:
        self.cdp.off("Target.targetCreated", self._on_target_created)
        self.cdp.off("Target.targetDestroyed", self._on_target_destroyed)
        for extra in self.opened:  # tabs this tab opened and the engine never followed are its to close too
            try:
                await self.cdp.send("Target.closeTarget", {"targetId": extra}, timeout=5)
            except CDPError:
                pass
        try:
            await self.cdp.send("Target.closeTarget", {"targetId": self.target_id}, timeout=5)
        except CDPError:
            pass
