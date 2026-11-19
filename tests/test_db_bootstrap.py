from types import SimpleNamespace

import pytest
from sqlalchemy import URL
from sqlalchemy.dialects.postgresql import dialect

from vpi_bot import db_bootstrap


@pytest.mark.asyncio
async def test_sqlite_database_needs_no_server_bootstrap():
    assert await db_bootstrap.ensure_database_exists("sqlite+aiosqlite:///./unused.db") is False


@pytest.mark.asyncio
async def test_postgresql_database_is_created_once_with_quoted_name(monkeypatch):
    class FakeConnection:
        def __init__(self, engine):
            self.engine = engine

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def execution_options(self, **kwargs):
            assert kwargs == {"isolation_level": "AUTOCOMMIT"}
            return self

        async def scalar(self, query, params):
            assert params["database_name"] == 'vpi"bot'
            return 1 if self.engine.state["exists"] else None

        async def exec_driver_sql(self, sql):
            self.engine.create_sql = sql
            self.engine.state["exists"] = True

    state = {"exists": False}

    class FakeEngine:
        def __init__(self):
            self.state = state
            self.create_sql = None
            self.disposed = False
            self.sync_engine = SimpleNamespace(dialect=dialect())

        def connect(self):
            return FakeConnection(self)

        async def dispose(self):
            self.disposed = True

    created_engines = []

    def fake_create_async_engine(url, poolclass):
        assert url.database == "postgres"
        engine = FakeEngine()
        created_engines.append(engine)
        return engine

    monkeypatch.setattr(db_bootstrap, "create_async_engine", fake_create_async_engine)
    url = URL.create(
        "postgresql+asyncpg",
        username="vpi",
        password="test-password",
        host="localhost",
        port=5432,
        database='vpi"bot',
    )

    assert await db_bootstrap.ensure_database_exists(url) is True
    assert created_engines[0].create_sql == 'CREATE DATABASE "vpi""bot"'
    assert created_engines[0].disposed
    assert await db_bootstrap.ensure_database_exists(url) is False
    assert created_engines[1].create_sql is None
    assert created_engines[1].disposed
