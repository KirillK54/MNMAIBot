import asyncio
import os
from datetime import UTC, date, datetime
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from vpi_bot.db_bootstrap import ensure_database_exists
from vpi_bot.economy import calculate_daily_economy
from vpi_bot.models import (
    Base,
    Country,
    CountryResource,
    DailyRun,
    EconomicStats,
    GamePost,
    ResourceDefinition,
    ResourceOperation,
    ResourceProduction,
    Verdict,
    VerdictOutbox,
)
from vpi_bot.outbox import claim_due
from vpi_bot.post_processing import reclaim_failed_post
from vpi_bot.production import run_resource_production_cycle

POSTGRES_TEST_DATABASE_URL = os.getenv("POSTGRES_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not POSTGRES_TEST_DATABASE_URL,
    reason="set POSTGRES_TEST_DATABASE_URL to an isolated PostgreSQL test database",
)


@pytest.mark.asyncio
async def test_skip_locked_prevents_two_workers_claiming_same_outbox_row():
    if not POSTGRES_TEST_DATABASE_URL:
        pytest.skip("PostgreSQL integration URL is not configured")
    admin_engine = create_async_engine(POSTGRES_TEST_DATABASE_URL)
    schema = f"vpi_test_{uuid4().hex}"
    async with admin_engine.begin() as connection:
        await connection.execute(text(f'CREATE SCHEMA "{schema}"'))

    engine = create_async_engine(
        POSTGRES_TEST_DATABASE_URL,
        connect_args={"server_settings": {"search_path": schema}},
        pool_size=3,
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
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
                treasury=10,
                income=5,
                expenses=1,
                gdp=100,
                growth=0,
            )
            session.add(country)
            await session.flush()
            post = GamePost(
                guild_id=1,
                discord_message_id=3,
                channel_id=4,
                author_id=2,
                country_id=country.id,
                body="Тест",
                local_date=datetime.now(UTC).date(),
            )
            session.add(post)
            await session.flush()
            verdict = Verdict(
                post_id=post.id,
                status="success",
                title="Тест",
                description="Тест",
                proposed_changes={},
                applied_changes={},
                state="applied",
            )
            session.add(verdict)
            await session.flush()
            session.add(
                VerdictOutbox(
                    post_id=post.id,
                    verdict_id=verdict.id,
                    guild_id=1,
                    channel_id=4,
                    source_message_id=3,
                    embed_payload={"title": "Тест"},
                    status="pending",
                )
            )
            await session.commit()

        async with factory() as first, factory() as second:
            first_claim = await claim_due(first, limit=1)
            assert len(first_claim) == 1
            await first.flush()
            second_claim = await claim_due(second, limit=1)
            assert second_claim == []
            await second.rollback()
            await first.commit()
        async with factory() as session:
            row = await session.scalar(select(VerdictOutbox))
            assert row.status == "delivering" and row.attempts == 1
            country = await session.scalar(select(Country))
            failed_post = GamePost(
                guild_id=1,
                discord_message_id=5,
                channel_id=4,
                author_id=2,
                country_id=country.id,
                body="Первоначальный текст",
                local_date=datetime.now(UTC).date(),
                state="failed",
            )
            session.add(failed_post)
            await session.commit()
            failed_post_id, country_id = failed_post.id, country.id

        second_started = asyncio.Event()
        async with factory() as first:
            first_recovered = await reclaim_failed_post(
                first,
                guild_id=1,
                discord_message_id=5,
                country_id=country_id,
                channel_id=4,
                body="Повторный текст",
                local_date=datetime.now(UTC).date(),
            )
            assert first_recovered == failed_post_id
            await first.flush()

            async def concurrent_recovery():
                async with factory() as second:
                    second_started.set()
                    result = await reclaim_failed_post(
                        second,
                        guild_id=1,
                        discord_message_id=5,
                        country_id=country_id,
                        channel_id=4,
                        body="Дублирующий повтор",
                        local_date=datetime.now(UTC).date(),
                    )
                    await second.commit()
                    return result

            second_task = asyncio.create_task(concurrent_recovery())
            await second_started.wait()
            await first.commit()
            assert await second_task is None

        # EconomicStats is the serialization row; the second settlement must see the first DailyRun.
        daily_date = date(2026, 11, 18)
        daily_started = asyncio.Event()
        async with factory() as first:
            country = await first.get(Country, country_id)
            first_report = await calculate_daily_economy(first, country, daily_date)
            assert first_report is not None
            await first.flush()

            async def concurrent_settlement():
                async with factory() as second:
                    country = await second.get(Country, country_id)
                    daily_started.set()
                    report = await calculate_daily_economy(second, country, daily_date)
                    await second.commit()
                    return report

            daily_task = asyncio.create_task(concurrent_settlement())
            await daily_started.wait()
            await first.commit()
            assert await daily_task is None
        async with factory() as session:
            assert await session.scalar(select(DailyRun.id)) is not None
            stats = await session.get(EconomicStats, country_id)
            assert stats.treasury == 14

        async with factory() as session:
            resource = ResourceDefinition(guild_id=1, name="Железо")
            session.add(resource)
            await session.flush()
            production = ResourceProduction(
                country_id=country_id,
                resource_id=resource.id,
                amount_per_tick=2.5,
                interval_hours=4,
                enabled=True,
            )
            session.add(production)
            await session.commit()
            production_id, resource_id = production.id, resource.id

        production_started = asyncio.Event()
        tick_time = datetime.now(UTC)
        async with factory() as first:
            first_report = await run_resource_production_cycle(first, production_id, now=tick_time)
            assert first_report is not None
            await first.flush()

            async def concurrent_production():
                async with factory() as second:
                    production_started.set()
                    report = await run_resource_production_cycle(
                        second, production_id, now=tick_time
                    )
                    await second.commit()
                    return report

            production_task = asyncio.create_task(concurrent_production())
            await production_started.wait()
            await first.commit()
            assert await production_task is None
        async with factory() as session:
            inventory = await session.get(CountryResource, (country_id, resource_id))
            operations = (await session.scalars(select(ResourceOperation))).all()
            assert inventory.quantity == 2.5
            assert len(operations) == 1
    finally:
        await engine.dispose()
        async with admin_engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await admin_engine.dispose()


@pytest.mark.asyncio
async def test_postgres_bootstrap_creates_and_reuses_missing_database():
    base_url = make_url(POSTGRES_TEST_DATABASE_URL)
    database_name = f"vpi_bootstrap_{uuid4().hex}"
    target_url = base_url.set(database=database_name)
    target_engine = None
    cleanup_engine = create_async_engine(base_url.set(database="postgres"))
    try:
        assert await ensure_database_exists(target_url) is True
        target_engine = create_async_engine(target_url)
        async with target_engine.connect() as connection:
            assert await connection.scalar(text("SELECT 1")) == 1
        assert await ensure_database_exists(target_url) is False
    finally:
        if target_engine is not None:
            await target_engine.dispose()
        async with cleanup_engine.connect() as connection:
            connection = await connection.execution_options(isolation_level="AUTOCOMMIT")
            await connection.exec_driver_sql(f'DROP DATABASE IF EXISTS "{database_name}"')
        await cleanup_engine.dispose()
