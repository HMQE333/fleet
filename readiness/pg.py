"""A throwaway Postgres for tests and readiness runs, from the `pgserver` package (no service to install).

`start(dir)` returns a DSN whose database is `fleet_test`; tests/conftest.py drops and recreates that database on every
run, so a stale schema can never fake a result. The server keeps running until `stop()` (or the process ends).
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

_server = None


def available() -> bool:
    try:
        import pgserver  # noqa: F401
    except ImportError:
        return False
    return True


def with_database(uri: str, database: str) -> str:
    """The same server, another database name (pgserver's URI may carry a `?host=` socket query)."""
    u = urlsplit(uri)
    return urlunsplit((u.scheme, u.netloc, f"/{database}", u.query, u.fragment))


def start(pgdata: Path, database: str = "fleet_test") -> str:
    global _server
    import pgserver

    pgdata.mkdir(parents=True, exist_ok=True)
    _server = pgserver.get_server(str(pgdata))
    uri = _server.get_uri()
    return with_database(uri, database)


def stop() -> None:
    global _server
    if _server is not None:
        try:
            _server.cleanup()
        finally:
            _server = None
