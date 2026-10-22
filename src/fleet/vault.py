"""Encrypted storage for API keys the operator pastes into the panel.

- Values are encrypted with AES-256-GCM; the secret's name is bound in as associated data,
  so a ciphertext can't be moved to another name.
- The master key never touches the database: it comes from FLEET_MASTER_KEY (cloud) or the
  OS credential store (Windows Credential Manager / macOS Keychain), created on first use.
- Values are write-only through the API: they can be replaced or deleted, never read back.
- Every value this process has decrypted is scrubbed from events before they're stored.
"""

import base64
import hashlib
import logging
import os
import re
import time

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from fleet import db

log = logging.getLogger("fleet.vault")

KEYRING_SERVICE = "fleet"
KEYRING_USER = "master-key"
NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")
CACHE_SECONDS = 60

# The keys Fleet knows how to use. Each entry: what it's for, and whether Fleet can run without it.
KNOWN = {
    "OPENROUTER_API_KEY": {"label": "OpenRouter", "required": True,
                           "help": "Runs every AI model (planner, workers, research, video). openrouter.ai/keys"},
    "SIMPLELOGIN_API_KEY": {"label": "SimpleLogin", "required": False,
                            "help": "Creates email aliases for sign-ups (Fleet asks for one when it fills a form). "
                                    "simplelogin.io → Settings → API Key."},
    "ANTHROPIC_API_KEY": {"label": "Claude (Anthropic)", "required": False,
                          "help": "Only to run the planner or a reader on Anthropic's own API instead of "
                                  "OpenRouter — pick an “anthropic-api/…” model in Settings → Models. "
                                  "console.anthropic.com/settings/keys"},
    "ADSPOWER_API_KEY": {"label": "AdsPower", "required": False,
                         "help": "Only if you turned on API-key verification in AdsPower → Settings → API."},
}


class VaultError(RuntimeError):
    pass


_master: bytes | None = None
_cache: dict[str, tuple[str, float]] = {}
_seen_values: dict[str, str] = {}  # value -> name, for redaction


def _load_master_key() -> bytes:
    global _master
    if _master is not None:
        return _master
    env = os.environ.get("FLEET_MASTER_KEY")
    if env:
        try:
            key = base64.b64decode(env, validate=True)
        except ValueError as e:
            raise VaultError("FLEET_MASTER_KEY is not valid base64") from e
        if len(key) != 32:
            raise VaultError("FLEET_MASTER_KEY must decode to 32 bytes")
        _master = key
        return key
    try:
        import keyring

        stored = keyring.get_password(KEYRING_SERVICE, KEYRING_USER)
        if stored is None:
            stored = base64.b64encode(AESGCM.generate_key(bit_length=256)).decode()
            keyring.set_password(KEYRING_SERVICE, KEYRING_USER, stored)
            log.info("created a new master key in the OS credential store")
        _master = base64.b64decode(stored)
        return _master
    except Exception as e:  # no usable OS keyring (e.g. a headless server)
        raise VaultError(
            "No master key available. Set FLEET_MASTER_KEY to 32 random bytes in base64 "
            "(python -c \"import os,base64;print(base64.b64encode(os.urandom(32)).decode())\")"
        ) from e


def key_id(key: bytes) -> str:
    return hashlib.sha256(key).hexdigest()[:12]


def _hint(value: str) -> str:
    return f"…{value[-4:]}" if len(value) >= 12 else ""


def _validate_name(name: str) -> None:
    if not NAME_RE.match(name):
        raise VaultError("Secret names use A-Z, 0-9 and _, and start with a letter")


def encrypt(name: str, value: str, key: bytes) -> tuple[bytes, bytes]:
    nonce = os.urandom(12)
    return AESGCM(key).encrypt(nonce, value.encode("utf-8"), name.encode()), nonce


def decrypt(name: str, ciphertext: bytes, nonce: bytes, key: bytes) -> str:
    try:
        return AESGCM(key).decrypt(nonce, ciphertext, name.encode()).decode("utf-8")
    except InvalidTag as e:
        raise VaultError(f"{name} can't be decrypted (it was saved with a different master key, or it was tampered with)") from e


def remember(value: str, name: str) -> None:
    """Make redact() scrub this value from everything Fleet records from now on."""
    if len(value) >= 8:  # scrubbing very short strings would mangle ordinary text
        _seen_values[value] = name


def seal(aad: str, value: str) -> tuple[bytes, bytes, str]:
    """Encrypt a value for another table (site logins). `aad` ties the ciphertext to its row."""
    key = _load_master_key()
    ciphertext, nonce = encrypt(aad, value, key)
    return ciphertext, nonce, key_id(key)


def unseal(aad: str, ciphertext: bytes, nonce: bytes, *, redact_as: str = "SITE_LOGIN_PASSWORD") -> str:
    value = decrypt(aad, bytes(ciphertext), bytes(nonce), _load_master_key())
    remember(value, redact_as)
    return value


async def put(name: str, value: str) -> dict:
    _validate_name(name)
    value = value.strip()
    if not value:
        raise VaultError("The value is empty")
    if len(value) > 8192:
        raise VaultError("The value is too long")
    key = _load_master_key()
    ciphertext, nonce = encrypt(name, value, key)
    p = await db.pool()
    r = await p.fetchrow(
        """
        insert into secrets (name, ciphertext, nonce, key_id, hint) values ($1, $2, $3, $4, $5)
        on conflict (name) do update set ciphertext = excluded.ciphertext, nonce = excluded.nonce,
            key_id = excluded.key_id, hint = excluded.hint, updated_at = now()
        returning name, hint, created_at, updated_at, last_used_at
        """,
        name, ciphertext, nonce, key_id(key), _hint(value),
    )
    _cache.pop(name, None)
    return dict(r)


async def delete(name: str) -> bool:
    p = await db.pool()
    _cache.pop(name, None)
    return (await p.execute("delete from secrets where name = $1", name)) == "DELETE 1"


async def list_metadata() -> list[dict]:
    """Names, hints and dates only — never values."""
    p = await db.pool()
    rows = {r["name"]: dict(r) for r in await p.fetch(
        "select name, hint, created_at, updated_at, last_used_at from secrets order by name")}
    out = []
    for name, info in KNOWN.items():
        out.append({"name": name, **info, "set": name in rows, **({k: v for k, v in rows.get(name, {}).items() if k != "name"})})
    for name, row in rows.items():
        if name not in KNOWN:
            out.append({"name": name, "label": name, "required": False, "help": "", "set": True, **row})
    return out


async def get(name: str) -> str | None:
    """Decrypted value, or None when not set. Falls back to an environment variable of the same name."""
    cached = _cache.get(name)
    if cached and cached[1] > time.monotonic():
        return cached[0]
    p = await db.pool()
    r = await p.fetchrow("select ciphertext, nonce from secrets where name = $1", name)
    if r is None:
        value = os.environ.get(name) or None
    else:
        value = decrypt(name, r["ciphertext"], r["nonce"], _load_master_key())
        await p.execute("update secrets set last_used_at = now() where name = $1", name)
    if value:
        _cache[name] = (value, time.monotonic() + CACHE_SECONDS)
        if len(value) >= 8:  # scrubbing very short strings would mangle ordinary text
            _seen_values[value] = name
    return value


def redact(text: str) -> str:
    for value, name in _seen_values.items():
        if value in text:
            text = text.replace(value, f"[{name}]")
    return text


def redact_obj(obj):
    if isinstance(obj, str):
        return redact(obj)
    if isinstance(obj, dict):
        return {k: redact_obj(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact_obj(v) for v in obj]
    return obj
