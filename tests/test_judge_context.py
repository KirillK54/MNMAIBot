from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from vpi_bot.judge import build_context
from vpi_bot.models import Base, Country, CountryLore, EconomicStats, Epoch, GuildConfig


@pytest.mark.asyncio
async def test_context_selects_relevant_lore_and_includes_live_state(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'context.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        country = Country(
            guild_id=77,
            owner_id=88,
            name="Республика",
            government="Республика",
            religion="Светская",
            currency="крон",
        )
        country.economy = EconomicStats(treasury=Decimal(1234), gdp=Decimal(9000))
        session.add(country)
        await session.flush()
        session.add_all(
            [
                CountryLore(
                    country_id=country.id,
                    category="history",
                    title="Гражданская война",
                    content="Последствия гражданской войны повлияли на реформу земель.",
                    importance=1,
                ),
                CountryLore(
                    country_id=country.id,
                    category="geography",
                    title="Реки",
                    content="Северная речная система.",
                    importance=10,
                ),
            ]
        )
        config = GuildConfig(guild_id=77, epoch_name="Индустриальная эпоха", game_rules="Без магии")
        session.add_all(
            [
                config,
                Epoch(
                    guild_id=77,
                    name="Индустриальная эпоха",
                    description="Пар и фабрики.",
                    rules="Нет цифровых сетей.",
                    technologies=["паровая машина"],
                    restrictions={"forbidden_technologies": ["компьютер"]},
                    starting_stats={"economy": {"industry_level": 3}},
                ),
            ]
        )
        await session.flush()
        context = await build_context(session, country, config, "послевоенная земельная реформа")
        assert context["lore"][0]["title"] == "Гражданская война"
        assert Decimal(context["economy"]["treasury"]) == Decimal(1234)
        assert context["epoch"]["name"] == "Индустриальная эпоха"
        assert context["epoch"]["rules"] == "Нет цифровых сетей."
        assert context["epoch"]["available_technologies"] == ["паровая машина"]
        assert context["epoch"]["restrictions"]["forbidden_technologies"] == ["компьютер"]
        assert context["world_rules"] == "Без магии"
    await engine.dispose()
