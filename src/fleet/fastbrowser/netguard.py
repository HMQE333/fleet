"""Where the fast browser may go. Ported from JevBrowser's public-URL guard (MIT, see NOTICE.md).

A prompt-injected page must not steer the agent into the operator's own machine or network:
Fleet's API (127.0.0.1:8710), AdsPower's Local API (local.adspower.net → 127.0.0.1:50325), a router,
or cloud metadata endpoints. Only public http(s) on ports 80/443, after DNS resolution.
"""

import asyncio
import ipaddress
import socket
from urllib.parse import urlsplit

PRIVATE_SUFFIXES = (".localhost", ".local", ".internal", ".home", ".lan", ".arpa")


def _is_public_ip(value: str) -> bool:
    try:
        return ipaddress.ip_address(value).is_global
    except ValueError:
        return False


def check_url(url: str) -> str | None:
    """Reason the URL is off-limits, or None. No DNS (see check_url_resolved)."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return "malformed URL"
    if parts.scheme not in ("http", "https"):
        return f"only http(s) pages are allowed, not {parts.scheme or 'this'}"
    if parts.username or parts.password:
        return "URLs with embedded credentials are blocked"
    host = (parts.hostname or "").lower()
    if not host:
        return "URL has no host"
    if host == "localhost" or host.endswith(PRIVATE_SUFFIXES):
        return f"{host} is a private hostname"
    try:
        ipaddress.ip_address(host)
        if not _is_public_ip(host):
            return f"{host} is a private address"
    except ValueError:
        pass  # a name, checked after DNS
    if port is not None and port not in (80, 443):
        return f"port {port} is not a public web port"
    return None


async def check_url_resolved(url: str) -> str | None:
    """check_url + every DNS answer must be public (blocks names that point at 127.0.0.1)."""
    reason = check_url(url)
    if reason:
        return reason
    host = urlsplit(url).hostname or ""
    try:
        ipaddress.ip_address(host)
        return None  # literal public IP already checked
    except ValueError:
        pass
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except OSError:
        return None  # unresolvable here; the browser will show its own error page
    for info in infos:
        address = info[4][0]
        if not _is_public_ip(address):
            return f"{host} points to a private address ({address})"
    return None


def host_allowed(url: str, allowed: list[str]) -> bool:
    """True when no allowlist is set, or the host is an allowed domain or one of its subdomains."""
    if not allowed:
        return True
    host = (urlsplit(url).hostname or "").lower()
    return any(host == d or host.endswith("." + d) for d in allowed)
