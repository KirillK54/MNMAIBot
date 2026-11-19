from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from vpi_bot.models import Base, Country
from vpi_bot.ui.views import CountryPanel, CountrySelectView


def test_country_panel_and_selector_fit_discord_component_limits():
    bot = SimpleNamespace()
    countries = [Country(id=i, name=f"Государство {i}") for i in range(1, 31)]
    selector = CountrySelectView(bot, owner_id=999, countries=countries)
    assert len(selector.children) == 1
    assert len(selector.children[0].options) == 25
    panel = CountryPanel(bot, country_id=1, owner_id=999)
    assert len(panel.children) == 10
    assert any(getattr(child, "label", None) == "Как играть" for child in panel.children)
    assert all(child.row is None or child.row in {0, 1, 2, 3} for child in panel.children)


@pytest.mark.asyncio
async def test_admin_country_panel_allows_configured_admin_and_rejects_other_users(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'admin-country-view.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        country = Country(
            guild_id=10,
            owner_id=100,
            name="Пример",
            government="Республика",
            religion="Нет",
            currency="талер",
        )
        session.add(country)
        await session.commit()
        country_id = country.id

    async def guild_config(_guild_id):
        return SimpleNamespace(admin_role_id=55)

    bot = SimpleNamespace(db=factory, guild_config=guild_config)
    responses = []

    async def send_message(content, **kwargs):
        responses.append(content)

    interaction = SimpleNamespace(
        user=SimpleNamespace(id=900, roles=[SimpleNamespace(id=55)]),
        guild_id=10,
        permissions=SimpleNamespace(administrator=False),
        response=SimpleNamespace(send_message=send_message),
    )
    admin_panel = CountryPanel(bot, country_id, interaction.user.id, admin_access=True)
    assert await admin_panel.interaction_check(interaction)

    unauthorized = SimpleNamespace(
        user=SimpleNamespace(id=900, roles=[]),
        guild_id=10,
        permissions=SimpleNamespace(administrator=False),
        response=SimpleNamespace(send_message=send_message),
    )
    assert not await admin_panel.interaction_check(unauthorized)
    player_panel = CountryPanel(bot, country_id, unauthorized.user.id)
    assert not await player_panel.interaction_check(unauthorized)
    assert len(responses) == 2
    await engine.dispose()
