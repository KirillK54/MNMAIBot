from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from vpi_bot.models import AuditLog, Base, GuildConfig
from vpi_bot.ui.setup import (
    SetupChannelsView,
    SetupPreferencesModal,
    SetupRolesView,
    SetupWizardView,
    _save_field,
)


class FakeResponse:
    def __init__(self):
        self.kwargs = None

    async def edit_message(self, **kwargs):
        self.kwargs = kwargs


@pytest.mark.asyncio
async def test_setup_wizard_saves_selects_and_audits_them(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'setup-wizard.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        session.add(GuildConfig(guild_id=11))
        await session.commit()

    bot = SimpleNamespace(db=factory)
    response = FakeResponse()
    interaction = SimpleNamespace(
        guild_id=11,
        user=SimpleNamespace(id=22),
        response=response,
    )
    await _save_field(bot, interaction, "category_id", 33)

    async with factory() as session:
        config = await session.get(GuildConfig, 11)
        audit = await session.scalar(select(AuditLog).where(AuditLog.action == "setup_category_id"))
        assert config.category_id == 33
        assert audit.actor_id == 22
    assert response.kwargs["view"] is None
    await engine.dispose()


def test_setup_wizard_and_catalog_selects_build_with_discord_components():
    assert len(SetupWizardView(object(), 1, 2).children) == 5
    assert len(SetupChannelsView(object(), 2).children) == 3
    assert len(SetupRolesView(object(), 2).children) == 3
    assert len(SetupPreferencesModal(object(), 2, "Europe/Saratov", 3, False).children) == 3
