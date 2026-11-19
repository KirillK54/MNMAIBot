import pytest

from vpi_bot import database
from vpi_bot.bot import VPIBot
from vpi_bot.config import Settings


@pytest.mark.asyncio
async def test_bot_shutdown_disposes_database_engine():
    class FakeEngine:
        disposed = False

        async def dispose(self):
            self.disposed = True

    bot = VPIBot(Settings(), session_factory=object())
    engine = FakeEngine()
    bot.engine = engine
    await bot.close()
    assert engine.disposed


@pytest.mark.asyncio
async def test_setup_hook_reports_database_connection_failure(monkeypatch):
    class RefusingEngine:
        disposed = False
        url = "sqlite+aiosqlite:///unused.db"

        def begin(self):
            return self

        async def __aenter__(self):
            raise ConnectionRefusedError("no listener")

        async def __aexit__(self, *args):
            return None

        async def dispose(self):
            self.disposed = True

    engine = RefusingEngine()
    monkeypatch.setattr(database, "engine", engine)
    bot = VPIBot(Settings(), session_factory=object())
    with pytest.raises(SystemExit, match="DATABASE_URL"):
        await bot.setup_hook()
    await bot.close()
    assert engine.disposed
