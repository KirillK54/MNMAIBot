from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from vpi_bot.economy import calculate_daily_economy
from vpi_bot.models import Base, Country, DailyRun, EconomicStats


@pytest.mark.asyncio
async def test_daily_economy_settles_and_is_idempotent(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'economy.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        country = Country(
            guild_id=1,
            owner_id=2,
            name="Тест",
            government="Республика",
            religion="Нет",
            currency="рубль",
        )
        country.economy = EconomicStats(
            treasury=Decimal(100),
            income=Decimal(50),
            expenses=Decimal(70),
            debt=Decimal(0),
            gdp=Decimal(1000),
            growth=Decimal(2),
            inflation=Decimal(1),
            unemployment=Decimal(10),
            stability=Decimal(60),
        )
        session.add(country)
        await session.flush()
        first = await calculate_daily_economy(session, country, date(2026, 1, 1))
        await session.commit()
        assert Decimal(first["treasury_after"]) == Decimal(80)
        assert Decimal(first["debt_after"]) == Decimal(0)
        second = await calculate_daily_economy(session, country, date(2026, 1, 1))
        stats = await session.get(EconomicStats, country.id)
        runs = (await session.scalars(select(DailyRun))).all()
        assert second is None
        assert stats.treasury == Decimal("80.00")
        assert len(runs) == 1
    await engine.dispose()
