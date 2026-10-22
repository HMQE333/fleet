"""FLEET_DATABASE_URL=embedded: Fleet starts its own Postgres (pgserver) under data/pg; anything else is passed through."""

import sys
import types

import pytest

from fleet import db
from fleet.config import settings

pytestmark = pytest.mark.no_db


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    monkeypatch.setattr(db, "_embedded_dsn", None)
    yield
    monkeypatch.setattr(db, "_embedded_dsn", None)


async def test_a_plain_dsn_is_passed_through(monkeypatch):
    monkeypatch.setattr(settings(), "database_url", "postgresql://u:p@db.example:5433/fleet")
    assert await db.dsn() == "postgresql://u:p@db.example:5433/fleet"


async def test_embedded_starts_pgserver_once_under_data_pg(monkeypatch, tmp_path):
    starts = []

    class Server:
        def get_uri(self, database=None):
            return "postgresql://postgres:@127.0.0.1:54321/postgres"

    fake = types.ModuleType("pgserver")
    fake.get_server = lambda pgdata: starts.append(pgdata) or Server()
    monkeypatch.setitem(sys.modules, "pgserver", fake)
    monkeypatch.setattr(settings(), "database_url", "embedded")
    monkeypatch.setattr(settings(), "data_dir", tmp_path)
    assert await db.dsn() == "postgresql://postgres:@127.0.0.1:54321/fleet"
    assert await db.dsn() == "postgresql://postgres:@127.0.0.1:54321/fleet"
    assert starts == [str(tmp_path / "pg")] and (tmp_path / "pg").is_dir()


async def test_embedded_without_pgserver_says_what_to_install(monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, "pgserver", None)  # import fails
    monkeypatch.setattr(settings(), "database_url", "embedded")
    monkeypatch.setattr(settings(), "data_dir", tmp_path)
    with pytest.raises(db.DatabaseUnavailable, match="pip install pgserver"):
        await db.dsn()
