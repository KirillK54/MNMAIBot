from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from vpi_bot.models import (
    Base,
    Country,
    CountryResource,
    ResourceDefinition,
    ResourceOperation,
    ResourceProduction,
)
from vpi_bot.production import run_resource_production_cycle


@pytest.mark.asyncio
async def test_resource_production_cycle_is_idempotent_and_records_audit(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'production.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        country = Country(
            guild_id=5,
            owner_id=6,
            name="Тест",
            government="Республика",
            religion="Нет",
            currency="рубль",
        )
        resource = ResourceDefinition(guild_id=5, name="Железо", enabled=True)
        session.add_all([country, resource])
        await session.flush()
        production = ResourceProduction(
            country_id=country.id,
            resource_id=resource.id,
            amount_per_tick=Decimal("2.500"),
            interval_hours=4,
            enabled=True,
        )
        session.add(production)
        await session.flush()
        now = datetime(2026, 11, 18, 8, tzinfo=UTC)
        report = await run_resource_production_cycle(session, production.id, now=now)
        await session.commit()
        assert report == {
            "country_id": country.id,
            "country_name": "Тест",
            "resource_name": "Железо",
            "amount": "2.500",
            "before": "0",
            "after": "2.500",
        }
        assert await run_resource_production_cycle(session, production.id, now=now) is None
        inventory = await session.get(CountryResource, (country.id, resource.id))
        operations = (await session.scalars(select(ResourceOperation))).all()
        assert inventory.quantity == Decimal("2.500")
        assert len(operations) == 1
        assert operations[0].source_id == str(production.id)
    await engine.dispose()


@pytest.mark.asyncio
async def test_disabled_resource_does_not_produce(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'disabled-production.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        country = Country(
            guild_id=5,
            owner_id=6,
            name="Тест",
            government="Республика",
            religion="Нет",
            currency="рубль",
        )
        resource = ResourceDefinition(guild_id=5, name="Железо", enabled=False)
        session.add_all([country, resource])
        await session.flush()
        production = ResourceProduction(
            country_id=country.id, resource_id=resource.id, amount_per_tick=Decimal(1), enabled=True
        )
        session.add(production)
        await session.flush()
        result = await run_resource_production_cycle(session, production.id)
        assert result is None
        assert production.last_tick_at is None
        assert await session.scalar(select(ResourceOperation.id)) is None
    await engine.dispose()
