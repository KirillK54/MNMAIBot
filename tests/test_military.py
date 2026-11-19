from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from vpi_bot.game_engine import ChangeRejected
from vpi_bot.military import adjust_military
from vpi_bot.models import Base, Country, EconomicStats, MilitaryStats


@pytest.mark.asyncio
async def test_admin_can_expand_and_reduce_army_and_transfer_budget(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'military.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        country = Country(
            guild_id=42,
            owner_id=7,
            name="Тест",
            government="Республика",
            religion="Нет",
            currency="рубль",
        )
        country.economy = EconomicStats(treasury=Decimal(1000))
        session.add(country)
        await session.flush()

        await adjust_military(
            session,
            guild_id=42,
            country_id=country.id,
            section="metric",
            key="personnel",
            raw_delta=100,
        )
        await adjust_military(
            session, guild_id=42, country_id=country.id, section="units", key="пехота", raw_delta=12
        )
        await adjust_military(
            session, guild_id=42, country_id=country.id, section="units", key="пехота", raw_delta=-2
        )
        await adjust_military(
            session,
            guild_id=42,
            country_id=country.id,
            section="metric",
            key="budget",
            raw_delta=250,
        )
        army = await session.get(MilitaryStats, country.id)
        stats = await session.get(EconomicStats, country.id)
        assert army.personnel == 100
        assert army.units == {"пехота": 10.0}
        assert army.budget == Decimal(250)
        assert stats.treasury == Decimal(750)

        await adjust_military(
            session,
            guild_id=42,
            country_id=country.id,
            section="metric",
            key="budget",
            raw_delta=-50,
        )
        await adjust_military(
            session,
            guild_id=42,
            country_id=country.id,
            section="metric",
            key="personnel",
            raw_delta=-25,
        )
        assert army.budget == Decimal(200)
        assert army.personnel == 75
        assert stats.treasury == Decimal(800)
        with pytest.raises(ChangeRejected, match="не может быть отрицательным"):
            await adjust_military(
                session,
                guild_id=42,
                country_id=country.id,
                section="units",
                key="пехота",
                raw_delta=-100,
            )
        with pytest.raises(ChangeRejected, match="Недостаточно казны"):
            await adjust_military(
                session,
                guild_id=42,
                country_id=country.id,
                section="metric",
                key="budget",
                raw_delta=1000,
            )
    await engine.dispose()
