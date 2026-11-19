from types import SimpleNamespace

import discord
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from vpi_bot.epoch_service import apply_epoch_selection, refresh_country_epoch_cards
from vpi_bot.models import AuditLog, Base, Epoch, GuildConfig
from vpi_bot.ui.epoch_picker import open_epoch_picker


class FakeResponse:
    def __init__(self):
        self.kwargs = None
        self.messages = []

    async def send_message(self, content=None, **kwargs):
        self.messages.append(content)
        self.kwargs = kwargs


@pytest.mark.asyncio
async def test_epoch_picker_opens_with_dropdown_and_requires_admin(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'epoch-picker.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        session.add(GuildConfig(guild_id=55, epoch_name="Античность", admin_role_id=700))
        await session.commit()

    async def guild_config(guild_id):
        assert guild_id == 55
        return SimpleNamespace(epoch_name="Античность", admin_role_id=700)

    bot = SimpleNamespace(db=factory, guild_config=guild_config)
    response = FakeResponse()
    interaction = SimpleNamespace(
        guild_id=55,
        user=SimpleNamespace(id=10),
        response=response,
    )
    await open_epoch_picker(bot, interaction)
    view = response.kwargs["view"]
    selector = next(child for child in view.children if isinstance(child, discord.ui.Select))
    apply_button = next(
        child
        for child in view.children
        if isinstance(child, discord.ui.Button) and child.label == "Применить эпоху"
    )
    assert len(selector.options) == 12
    assert sum(option.default for option in selector.options) == 1
    assert apply_button.disabled
    assert response.kwargs["ephemeral"]

    admin_interaction = SimpleNamespace(
        user=SimpleNamespace(id=10, roles=[SimpleNamespace(id=700)]),
        guild_id=55,
        permissions=SimpleNamespace(administrator=False),
        response=FakeResponse(),
    )
    assert await view.interaction_check(admin_interaction)
    unauthorized = SimpleNamespace(
        user=SimpleNamespace(id=10, roles=[]),
        guild_id=55,
        permissions=SimpleNamespace(administrator=False),
        response=FakeResponse(),
    )
    assert not await view.interaction_check(unauthorized)
    assert unauthorized.response.messages
    await engine.dispose()


@pytest.mark.asyncio
async def test_graphical_epoch_apply_audits_choice_and_refreshes_cards(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'epoch-apply.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        session.add_all(
            [
                GuildConfig(guild_id=66, epoch_name="Старая эпоха"),
                Epoch(
                    id=1,
                    guild_id=66,
                    name="Каменный век",
                    rules="Без металла",
                    technologies=["камень"],
                    starting_stats={"economy": {"treasury": 250}},
                ),
            ]
        )
        await session.commit()

    bot = SimpleNamespace(db=factory)
    name, has_template = await apply_epoch_selection(bot, 66, 100, 1)
    assert name == "Каменный век" and has_template
    assert await refresh_country_epoch_cards(bot, 66) == (0, 0)
    async with factory() as session:
        config = await session.get(GuildConfig, 66)
        audit = await session.scalar(select(AuditLog).where(AuditLog.action == "epoch_set"))
        assert config.epoch_name == "Каменный век"
        assert audit.actor_id == 100
        assert '"name": "Каменный век"' in audit.new_value
    await engine.dispose()
