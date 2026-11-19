from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from vpi_bot.industry import run_facility_cycle
from vpi_bot.models import (
    Base,
    Country,
    CountryResource,
    EconomicOperation,
    EconomicStats,
    IndustryFacility,
    ResourceDefinition,
    ResourceOperation,
)


@pytest.mark.asyncio
async def test_facility_cycle_consumes_inputs_and_is_idempotent(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'industry.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        country = Country(
            guild_id=12,
            owner_id=21,
            name="Промышленная республика",
            government="Республика",
            religion="Нет",
            currency="талер",
        )
        country.economy = EconomicStats(treasury=Decimal(100), expenses=Decimal(10))
        iron = ResourceDefinition(guild_id=12, name="Железо")
        steel = ResourceDefinition(guild_id=12, name="Сталь")
        session.add_all([country, iron, steel])
        await session.flush()
        session.add_all(
            [
                CountryResource(country_id=country.id, resource_id=iron.id, quantity=Decimal(5)),
                CountryResource(country_id=country.id, resource_id=steel.id, quantity=Decimal(0)),
            ]
        )
        facility = IndustryFacility(
            country_id=country.id,
            name="Металлургический завод",
            facility_type="завод",
            input_per_tick={"Железо": 2},
            production_per_tick={"Сталь": 1},
            operating_cost=Decimal(5),
            interval_hours=4,
        )
        session.add(facility)
        await session.flush()
        now = datetime(2026, 11, 17, 8, tzinfo=UTC)
        report = await run_facility_cycle(session, facility, now=now)
        await session.commit()
        assert report["outputs"] == {"Сталь": "1"}
        assert (await session.get(CountryResource, (country.id, iron.id))).quantity == Decimal(3)
        assert (await session.get(CountryResource, (country.id, steel.id))).quantity == Decimal(1)
        assert (await session.get(EconomicStats, country.id)).treasury == Decimal(95)
        assert await run_facility_cycle(session, facility, now=now) is None
        assert len((await session.scalars(select(ResourceOperation))).all()) == 2
        assert len((await session.scalars(select(EconomicOperation))).all()) == 1
    await engine.dispose()
