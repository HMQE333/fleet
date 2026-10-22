"""Which clicks need the operator's OK (RULES.md §4). Deterministic and conservative.

The fast engine has no tool for "ask before acting", so code decides from the target's
accessible label. A false positive costs one approval click; a false negative could send or
pay without asking — so the lists lean towards asking. English and Polish.
"""

import re

# Strong words: anywhere in the label.
_ANYWHERE = (
    r"delete|remove|discard|publish|unsubscribe|purchase|checkout|check out|place order|pay now|"
    r"transfer|withdraw|donate|book now|reserve|save changes|"
    r"usuń|opublikuj|publikuj|zapłać|płacę|zamów|zamawiam|kupuję|przelew|zarezerwuj|zapisz zmiany"
)
# Verbs that are only commitments when the label starts with them ("Send", "Post now" — but not
# "More like this" or "Shipping and post").
_LEADING = (
    r"send|post|submit|share|tweet|retweet|repost|reply|comment|upload|follow|unfollow|like|upvote|vote|"
    r"subscribe|schedule|confirm|buy|order(?!\s+by)|pay|sign up|register|apply now|accept|agree|"
    r"wyślij|prześlij|udostępnij|skomentuj|odpowiedz|obserwuj|polub|zaplanuj|potwierdź|zatwierdź|kup|"
    r"zarejestruj|akceptuj|zgadzam"
)
_PAYMENT = (
    r"purchase|checkout|check out|place order|pay|payment|buy|order(?!\s+by)|subscribe|donate|transfer|"
    r"zapłać|płacę|płatność|zamów|zamawiam|kup|kupuję|subskrybuj|przelew"
)

# Never "always allow": can't be undone and rarely routine, or commits the operator to something (terms, sign-ups).
_NEVER_TRUST = (
    r"delete|remove|discard|deactivate|close account|cancel|unsubscribe|withdraw|transfer|donate|book|reserve|"
    r"save changes|sign up|register|accept|agree|terms|"
    r"usuń|przelew|zarezerwuj|zapisz zmiany|zarejestruj|akceptuj|zgadzam|anuluj"
)
# Labels whose meaning depends on the page around them (any confirm dialog says "Confirm").
_GENERIC = (
    r"ok|okay|yes|no|confirm|submit|continue|next|proceed|apply|done|save|go|"
    r"tak|nie|potwierdź|zatwierdź|dalej|zapisz"
)

IRREVERSIBLE = re.compile(rf"\b({_ANYWHERE})\b|^\s*({_LEADING})\b", re.IGNORECASE)
PAYMENT = re.compile(rf"^\s*({_PAYMENT})\b|\b(place order|pay now|checkout|check out|zapłać|płatność|kupuję)\b", re.IGNORECASE)


def needs_approval(kind: str, label: str) -> bool:
    """Clicks (and dropdown picks) whose label reads like a commitment."""
    if kind not in ("click", "select"):
        return False
    return bool(IRREVERSIBLE.search(label or ""))


def is_payment(label: str) -> bool:
    return bool(PAYMENT.search(label or ""))


def can_trust(label: str) -> bool:
    """May the operator say "always allow this click"? Never for payments, deleting, accepting terms, signing up
    or generic labels ("OK", "Confirm") that mean different things on different pages."""
    text = re.sub(r"\s+", " ", label or "").strip().lower()
    if not text or is_payment(text):
        return False
    return not (re.search(rf"\b({_NEVER_TRUST})\b", text) or re.fullmatch(rf"({_GENERIC})[.!]?", text))
