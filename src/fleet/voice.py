"""Voice: spoken words become browser commands, in Polish and English, without a model.

`parse(text)` is a hand-written grammar (regular expressions over a normalised utterance): no model, no cost, no network,
and it answers in microseconds, which is what lets a spoken "scroll down" beat the mouse. A `Command` has a `kind` and
`args`; `describe(command)` says it back in words. The grammar is deliberately small: one verb per command, the object
after it. Anything it doesn't understand returns None, and the panel asks again rather than guessing.

Secrets (RULES.md §1): an utterance that would type a password, a card number, a PIN or a CVV is never a `type` command;
it becomes `refused`, whose args carry a reason and never the words themselves, so neither the API nor a log sees them.

"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from urllib.parse import quote_plus

from fleet.fastbrowser import netguard, policy
from fleet.fastbrowser.cdp import CDP, CDPError
from fleet.fastbrowser.decide import filter_actions
from fleet.fastbrowser.engine import _norm
from fleet.fastbrowser.page import Page, StalePage

KINDS = ("open", "search", "click", "type", "scroll", "back", "forward", "tab", "read", "stop", "approve", "reject",
         "task", "help", "key", "refused")


@dataclass
class Command:
    kind: str
    args: dict = field(default_factory=dict)

    def __repr__(self) -> str:  # a refused command never repeats what was said
        return f"Command({self.kind}, {self.args})"

    def to_dict(self) -> dict:
        return {"kind": self.kind, **self.args}


# -- the grammar ----------------------------------------------------------------------------------------------------

# Words that mean the utterance carries a secret. "card" alone is not one ("open my card" could be a loyalty card page);
# a card is refused by the number itself.
_SECRET_WORDS = re.compile(
    r"\b(password|passwd|passcode|pin|cvv|cvc|card number|credit card|debit card|"
    r"has[łl]o|has[łl]a|has[łl]em|numer karty|kart[ąa] kredytow|kart[ąa] p[łl]atnicz|kod cvv|kod pin)\b",
    re.IGNORECASE,
)
# 13-19 digits with optional spaces or dashes between groups: a card number however it was read out.
_CARD_NUMBER = re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)")

_ORDINALS = {
    "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6, "seventh": 7, "eighth": 8, "ninth": 9,
    "tenth": 10,
    "pierwsz": 1, "drug": 2, "trzec": 3, "czwart": 4, "piąt": 5, "szóst": 6, "siódm": 7, "ósm": 8, "dziewiąt": 9,
    "dziesiąt": 10,
}
_WHAT = {
    "result": "result", "results": "result", "wynik": "result", "wyniku": "result",
    "link": "link", "linku": "link", "odnośnik": "link", "odnośnika": "link",
    "item": "item", "entry": "item", "pozycję": "item", "pozycja": "item", "element": "item",
    "product": "product", "produkt": "product", "offer": "product", "ofertę": "product", "oferta": "product",
    "article": "article", "artykuł": "article", "story": "article", "post": "article", "wpis": "article",
    "button": "button", "przycisk": "button", "tab": "tab", "kartę": "tab", "zakładkę": "tab",
    "page": "page", "stronę": "page", "strona": "page",
}
_ORDINAL_RE = "|".join(sorted(_ORDINALS, key=len, reverse=True))
_WHAT_RE = "|".join(sorted(_WHAT, key=len, reverse=True))
_NTH = re.compile(
    rf"^(?:the\s+)?(?:(?P<word>{_ORDINAL_RE})\w*|(?P<num>\d{{1,2}})\.?)\s+(?P<what>{_WHAT_RE})$", re.IGNORECASE
)

# a spoken address: "wikipedia.org", "allegro.pl/kategoria", "localhost:8710", or anything said with its scheme
_URL = re.compile(r"^(?:https?://\S+|(?:(?:[\w-]+\.)+[a-z]{2,}|localhost|\d{1,3}(?:\.\d{1,3}){3})(?::\d+)?(?:[/?#]\S*)?)$",
                  re.IGNORECASE)
_KEYS = {
    "enter": "Enter", "return": "Enter", "escape": "Escape", "esc": "Escape", "backspace": "Backspace",
    "delete": "Delete", "space": "Space", "spacja": "Space",
}

# (kind, pattern): the first match wins, so the specific ones come first.
_RULES: list[tuple[str, re.Pattern]] = [
    (rule[0], re.compile(rule[1], re.IGNORECASE)) for rule in (
        # one-word commands and their Polish equivalents
        ("help", r"^(?:help|pomoc|co (?:możesz|umiesz|potrafisz)(?: zrobić)?|what can you do)$"),
        ("stop", r"^(?:stop|zatrzymaj(?: się)?|przerwij|halt|cancel|anuluj)$"),
        ("key", r"^(?:(?:press|hit|naciśnij|wciśnij)\s+)?(?P<key>enter|return|escape|esc|backspace|delete|space|spacja)$"),
        ("key", r"^(?:zatwierdź|potwierdź)\s+enterem$"),
        ("approve", r"^(?:approve|approved|confirm|yes|ok|okay|go ahead|zatwierdź|zatwierdzam|potwierdź|potwierdzam|zgoda|zgadzam się|tak)$"),
        ("reject", r"^(?:reject|rejected|deny|no|nope|cancel it|odrzuć|odrzucam|nie|nie zgadzam się)$"),
        ("back", r"^(?:go back|back|wróć|cofnij|wstecz|do tyłu|poprzednia strona|previous page)$"),
        ("forward", r"^(?:go forward|forward|do przodu|naprzód|następna strona|next page)$"),
        ("tab", r"^(?:next tab|następna (?:karta|zakładka))$"),
        ("tab", r"^(?:previous tab|prev tab|last tab|poprzednia (?:karta|zakładka))$"),
        ("tab", r"^(?:tab|karta|zakładka)\s+(?P<n>\d{1,2})$"),
        ("read", r"^(?:read(?: it)?(?: to me)?(?: the page| this| it)?|read aloud|przeczytaj(?: to| stronę| mi)?|czytaj|odczytaj)$"),
        ("scroll", r"^(?:scroll(?: down)?|page down|scroll to the bottom|przewiń(?: w dół| na dół| niżej)?|niżej|w dół|na dół|dalej w dół)$"),
        ("scroll", r"^(?:scroll up|page up|scroll to the top|przewiń (?:w górę|do góry|wyżej|na górę)|wyżej|w górę|do góry)$"),
        ("task", r"^(?:new task|task|zadanie|nowe zadanie|zleć|zrób zadanie)[:,]?\s+(?P<prompt>.+)$"),
        ("open", r"^(?:open|go to|navigate to|visit|otwórz|wejdź na|przejdź (?:na|do)|idź (?:na|do)|pokaż)\s+(?P<what>.+)$"),
        ("search", r"^(?:search for|search|look up|find|szukaj|wyszukaj|poszukaj|znajdź)[:,]?\s+(?P<query>.+)$"),
        ("click", r"^(?:click on|click|press|tap|hit|select|choose|kliknij(?: w| na)?|naciśnij|wciśnij|wybierz)[:,]?\s+(?P<label>.+)$"),
        ("type", r"^(?:type in|type|enter text|write|wpisz|napisz|wprowadź)[:,]?\s+(?P<text>.+)$"),
    )
]

_LABEL_FILLER = re.compile(r"^(?:the|a|an|on|button|link|przycisk)\s+", re.IGNORECASE)


def normalise(text: str) -> str:
    """Whitespace folded, trailing punctuation dropped; the case stays (a query or a dictated note is the operator's)."""
    text = re.sub(r"\s+", " ", (text or "").strip())
    return text.rstrip(" .!?,;:").strip()


def _secret(text: str) -> bool:
    return bool(_SECRET_WORDS.search(text) or _CARD_NUMBER.search(text))


def _nth(text: str) -> dict | None:
    m = _NTH.match(text)
    if not m:
        return None
    n = int(m.group("num")) if m.group("num") else _ORDINALS[m.group("word").lower()]
    return {"nth": n, "what": _WHAT[m.group("what").lower()]}


def _url(text: str) -> str | None:
    text = text.strip().strip("\"'")
    if " " in text or not _URL.match(text):
        return None
    if not text.lower().startswith(("http://", "https://")):
        text = f"https://{text}"
    scheme, _, rest = text.partition("://")
    host, slash, path = rest.partition("/")
    return f"{scheme.lower()}://{host.lower()}{slash}{path}"


def parse(text: str) -> Command | None:
    """The command an utterance means, or None when the grammar doesn't understand it (the panel then asks again)."""
    said = normalise(text)
    if not said:
        return None
    for kind, pattern in _RULES:
        m = pattern.match(said)
        if not m:
            continue
        g = m.groupdict()
        if kind == "key":
            return Command("key", {"key": _KEYS.get((g.get("key") or "enter").lower(), "Enter")})
        if kind == "tab":
            if g.get("n"):
                return Command("tab", {"which": int(g["n"])})
            return Command("tab", {"which": "prev" if re.search(r"prev|last|poprzednia", said, re.IGNORECASE) else "next"})
        if kind == "scroll":
            return Command("scroll", {"direction": "up" if pattern.pattern.startswith("^(?:scroll up") else "down"})
        if kind == "task":
            prompt = g["prompt"].strip()
            if _CARD_NUMBER.search(prompt):
                return Command("refused", {"reason": "A task can't carry a card number."})
            return Command("task", {"prompt": prompt})
        if kind == "open":
            what = g["what"].strip()
            if url := _url(what):
                return Command("open", {"url": url})
            if nth := _nth(what):
                return Command("click", nth)
            return Command("click", {"label": _LABEL_FILLER.sub("", what)})
        if kind == "search":
            query = g["query"].strip()
            if _secret(query):
                return Command("refused", {"reason": "A search can't carry a password or a card number."})
            return Command("search", {"query": query})
        if kind == "click":
            label = g["label"].strip()
            if nth := _nth(label):
                return Command("click", nth)
            return Command("click", {"label": _LABEL_FILLER.sub("", label)})
        if kind == "type":
            if _secret(g["text"]):
                return Command("refused", {"reason": "Fleet never types a password, a PIN or a card number: use the vault."})
            return Command("type", {"text": g["text"].strip()})
        return Command(kind)
    return None


HELP = ("Say: open a site, search for something, click a label, open the third result, type words, scroll down or up, "
        "go back, next tab, read the page, approve, reject, stop, or new task and what to do.")


def describe(cmd: Command | None) -> str:
    """The command in words: what the panel reads back before or after acting."""
    if cmd is None:
        return "I didn't understand that."
    a = cmd.args
    match cmd.kind:
        case "open":
            return f"Opening {a['url']}"
        case "search":
            return f"Searching for {a['query']}"
        case "click":
            return f"Clicking {a['label']}" if a.get("label") else f"Opening the {_ordinal(a['nth'])} {a['what']}"
        case "type":
            return "Typing"  # never the words themselves (a dictated note is the operator's)
        case "scroll":
            return f"Scrolling {a['direction']}"
        case "tab":
            return f"Switching to the {a['which']} tab" if isinstance(a["which"], str) else f"Switching to tab {a['which']}"
        case "task":
            return f"New task: {a['prompt']}"
        case "key":
            return f"Pressing {a['key']}"
        case "refused":
            return a.get("reason", "Refused.")
        case "help":
            return HELP
        case _:
            return cmd.kind.capitalize()


def _ordinal(n: int) -> str:
    return {1: "first", 2: "second", 3: "third"}.get(n, f"{n}th")


# -- a live session (gate V4) ---------------------------------------------------------------------------------------


class VoiceError(RuntimeError):
    """A command that can't run here; the message is what the panel says."""


_SEARCH_FIELD = re.compile(r"search|szukaj|wyszuk|query|find|\bq\b", re.IGNORECASE)
_NTH_ROLES = {"button": ("button",), "tab": ("tab",)}  # everything else ("result", "link", "product"...) is a link


class Session:
    """One page of a browser Fleet reached over CDP, driven by parsed commands.

    Built on the fast engine's Page (observe, act, back) and its label matching: a spoken "click Next page" is matched
    against the labels the snapshot reads from the page, exactly first, then by prefix, then by containment. No model is
    called, so a navigation command runs in the time of one observe and one act (gate V4: under a second).

    The same gates as the engine, in code: a click whose label reads like a commitment (policy.needs_approval: send, pay,
    delete...) is never clicked on a spoken word alone; it waits for "approve" (or "reject"), and nothing is ever typed into
    a password field (the snapshot doesn't even list them, and `type` checks the focused element). A start page or an
    "open" that resolves to a private address is refused unless `allow_private` (tests only, as in the engine).
    """

    def __init__(self, cdp: CDP, page: Page, *, allow_private: bool = False):
        self.cdp = cdp
        self.page = page
        self.allow_private = allow_private
        self.pending: dict | None = None  # a commitment click that waits for "approve"
        self.history: list[dict] = []  # what ran: kind, label, ok, took

    @classmethod
    async def open(cls, cdp: CDP, url: str, *, allow_private: bool = False) -> Session:
        await cls._check_url(url, allow_private)
        page = await Page.open(cdp, url)
        return cls(cdp, page, allow_private=allow_private)

    @staticmethod
    async def _check_url(url: str, allow_private: bool) -> None:
        if not allow_private and (reason := await netguard.check_url_resolved(url)):
            raise VoiceError(f"Refused: {reason}")

    async def close(self) -> None:
        await self.page.close()

    async def observe(self) -> dict:
        return await self.page.observe(screenshot=False, settle_seconds=3.0)

    async def page_text(self) -> str:
        return (await self.observe()).get("text") or ""

    async def execute(self, cmd: Command) -> dict:
        """Run one command; always answers {"ok", "said", "took"} (and "needs_approval" for a click that waits)."""
        started = time.perf_counter()
        try:
            result = await self._run(cmd)
        except VoiceError as e:
            result = {"ok": False, "said": str(e)}
        except (TimeoutError, StalePage, CDPError, RuntimeError, ValueError) as e:
            result = {"ok": False, "said": f"That didn't work: {e}"}
        result.setdefault("ok", True)
        result.setdefault("said", describe(cmd))
        result["took"] = round(time.perf_counter() - started, 3)
        self.history.append({"kind": cmd.kind, "label": cmd.args.get("label"), "ok": result["ok"], "took": result["took"]})
        return result

    async def _run(self, cmd: Command) -> dict:
        a = cmd.args
        match cmd.kind:
            case "open":
                await self._check_url(a["url"], self.allow_private)
                await self.page.call("Page.navigate", url=a["url"])
                await self.page.wait_ready(10)
                return {"said": f"Opened {await self._title() or a['url']}"}
            case "search":
                return await self._search(a["query"])
            case "click":
                return await self._click(a)
            case "approve":
                return await self._approve()
            case "reject":
                waited = self.pending
                self.pending = None
                return {"said": f"Not clicking “{waited['label']}”." if waited else "Nothing waits for approval."}
            case "type":
                return await self._type(a["text"])
            case "key":
                await self._key(a["key"])
                return {}
            case "scroll":
                state = await self.observe()
                action = next((x for x in state["actions"] if x["id"] == f"scroll_{a['direction']}"), None)
                if action is None:
                    return {"said": "Already at the top." if a["direction"] == "up" else "Already at the bottom."}
                await self.page.act(action, state)
                return {}
            case "back":
                await self.page.back()
                return {"said": f"Back at {await self._title()}"}
            case "forward":
                try:
                    await self.page.evaluate("history.forward()")
                except StalePage:
                    pass
                await asyncio.sleep(0.3)
                await self.page.wait_ready(10)
                return {"said": f"Forward to {await self._title()}"}
            case "tab":
                return await self._tab(a["which"])
            case "read":
                text = re.sub(r"\s+", " ", await self.page_text()).strip()
                return {"text": text, "said": text[:1200] or "The page is empty."}
            case "stop":
                self.pending = None
                return {"said": "Stopped."}
            case "help":
                return {"said": HELP}
            case "task":
                raise VoiceError("A task runs through the planner, not this page: say it to the task box.")
            case "refused":
                raise VoiceError(a.get("reason", "Refused."))
        raise VoiceError(f"I can't do “{cmd.kind}” here.")

    async def _title(self) -> str:
        try:
            return (await self.page.evaluate("document.title") or "").strip()
        except (StalePage, CDPError):
            return ""

    # -- clicks: label matching like the engine's, approval like the engine's --------------------------------------

    @staticmethod
    def find_click(state: dict, args: dict) -> dict | None:
        """The observed click action a spoken "click X" / "open the third result" means, or None."""
        clicks = [x for x in state["actions"] if x["kind"] == "click"]
        if args.get("nth"):
            roles = _NTH_ROLES.get(args.get("what"), ("link",))
            picks = [x for x in filter_actions(clicks) if x.get("role") in roles]
            return picks[args["nth"] - 1] if 0 < args["nth"] <= len(picks) else None
        spoken = _norm(args.get("label"))
        if not spoken:
            return None
        words = spoken.split()
        for test in (lambda lb: lb == spoken, lambda lb: lb.startswith(spoken), lambda lb: spoken in lb,
                     lambda lb: all(w in lb.split() for w in words)):
            if found := next((x for x in clicks if test(_norm(x["label"]))), None):
                return found
        return None

    async def _click(self, args: dict) -> dict:
        state = await self.observe()
        action = self.find_click(state, args)
        if action is None:
            what = args.get("label") or f"the {_ordinal(args['nth'])} {args['what']}"
            return {"ok": False, "said": f"I can't see “{what}” on this page."}
        label = action["label"]
        if policy.needs_approval("click", label):
            self.pending = {"action": action, "state": state, "label": label}
            return {"needs_approval": True, "label": label, "said": f"“{label}” needs your approval: say approve or reject."}
        await self.page.act(action, state)
        return {"said": f"Clicked {label}"}

    async def _approve(self) -> dict:
        waited, self.pending = self.pending, None
        if waited is None:
            return {"said": "Nothing waits for approval."}
        if not await self.page.fresh(waited["state"], waited["action"]):
            return {"ok": False, "said": f"The page changed since “{waited['label']}”; say the click again."}
        await self.page.act(waited["action"], waited["state"])
        return {"said": f"Clicked {waited['label']}"}

    # -- typing: never a password field -------------------------------------------------------------------------------

    async def _focused(self) -> dict | None:
        return await self.page.evaluate(
            "(() => { const e = document.activeElement; if (!e) return null;"
            " const editable = e.isContentEditable || ['INPUT','TEXTAREA'].includes(e.tagName);"
            " return {editable, type: (e.type || '').toLowerCase(), autocomplete: e.getAttribute('autocomplete') || ''}; })()"
        )

    async def _type(self, text: str) -> dict:
        focused = await self._focused()
        if focused and focused["editable"]:
            if focused["type"] == "password" or focused["autocomplete"].startswith(("cc-", "one-time-code")):
                raise VoiceError("That's a password or card field: Fleet never types a secret from a spoken word.")
            await self.page.call("Input.insertText", text=text)
            return {"said": "Typed."}
        state = await self.observe()
        field = next((x for x in state["actions"] if x["kind"] == "fill"), None)  # the snapshot lists no password fields
        if field is None:
            return {"ok": False, "said": "Nothing to type into: click a field first."}
        await self.page.act(field, state, text)
        return {"said": f"Typed into {field['label']}."}

    async def _key(self, key: str) -> None:
        codes = {"Enter": (13, "\r"), "Escape": (27, ""), "Backspace": (8, ""), "Delete": (46, ""), "Space": (32, " ")}
        code, text = codes.get(key, (0, ""))
        down = {"type": "keyDown", "key": key, "code": key, "windowsVirtualKeyCode": code, "nativeVirtualKeyCode": code}
        if text:
            down["text"] = text
        await self.page.call("Input.dispatchKeyEvent", **down)
        await self.page.call("Input.dispatchKeyEvent", type="keyUp", key=key, code=key, windowsVirtualKeyCode=code)
        await asyncio.sleep(0.1)
        await self.page.wait_ready(5)

    async def _search(self, query: str) -> dict:
        state = await self.observe()
        fields = [x for x in state["actions"] if x["kind"] == "fill"]
        box = next((x for x in fields if x.get("role") == "searchbox" or _SEARCH_FIELD.search(x["label"])), None)
        if box is None:
            url = f"https://duckduckgo.com/?q={quote_plus(query)}"
            await self._check_url(url, self.allow_private)
            await self.page.call("Page.navigate", url=url)
            await self.page.wait_ready(10)
            return {"said": f"Searching the web for {query}"}
        await self.page.act(box, state, query)
        await self._key("Enter")
        return {"said": f"Searching for {query}"}

    # -- tabs -----------------------------------------------------------------------------------------------------------

    async def _tab(self, which: int | str) -> dict:
        targets = (await self.cdp.send("Target.getTargets"))["targetInfos"]
        pages = [t for t in targets if t.get("type") == "page" and not t.get("url", "").startswith("devtools://")]
        if not pages:
            raise VoiceError("No tabs.")
        ids = [t["targetId"] for t in pages]
        here = ids.index(self.page.target_id) if self.page.target_id in ids else 0
        if isinstance(which, int):
            if not 0 < which <= len(pages):
                raise VoiceError(f"There are {len(pages)} tabs.")
            index = which - 1
        else:
            index = (here + (1 if which == "next" else -1)) % len(pages)
        target = pages[index]
        if target["targetId"] != self.page.target_id:
            session = (await self.cdp.send("Target.attachToTarget", {"targetId": target["targetId"], "flatten": True}))["sessionId"]
            self.page = Page(self.cdp, target["targetId"], session)
            self.pending = None
        await self.cdp.send("Target.activateTarget", {"targetId": target["targetId"]})
        return {"said": f"Tab {index + 1} of {len(pages)}: {target.get('title') or target.get('url')}"}


class Live:
    """A session with its own CDP connection to a browser Fleet opened (the API keeps one per voice session)."""

    def __init__(self, cdp: CDP, session: Session, *, profile_id: str | None = None):
        self.cdp = cdp
        self.session = session
        self.profile_id = profile_id

    @classmethod
    async def connect(cls, ws_url: str, url: str, *, allow_private: bool = False, profile_id: str | None = None) -> Live:
        cdp = await CDP(ws_url).__aenter__()
        try:
            session = await Session.open(cdp, url, allow_private=allow_private)
        except BaseException:
            await cdp.__aexit__(None, None, None)
            raise
        return cls(cdp, session, profile_id=profile_id)

    async def close(self) -> None:
        try:
            await self.session.close()
        finally:
            await self.cdp.__aexit__(None, None, None)

