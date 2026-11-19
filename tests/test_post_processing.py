from datetime import UTC, date, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from vpi_bot.models import Base, Country, GamePost
from vpi_bot.post_processing import reclaim_failed_post


@pytest.mark.asyncio
async def test_failed_post_is_reclaimed_once_and_refreshes_message_data(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'post-recovery.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        country = Country(
            guild_id=8,
            owner_id=9,
            name="Тест",
            government="Республика",
            religion="Нет",
            currency="рубль",
        )
        session.add(country)
        await session.flush()
        original = GamePost(
            guild_id=8,
            discord_message_id=10,
            channel_id=11,
            author_id=9,
            country_id=country.id,
            body="Старый текст",
            local_date=date(2026, 1, 1),
            state="failed",
            processing_started_at=datetime.now(UTC),
            attempts=1,
        )
        session.add(original)
        await session.commit()

        claimed_id = await reclaim_failed_post(
            session,
            guild_id=8,
            discord_message_id=10,
            country_id=country.id,
            channel_id=12,
            body="Обновлённый текст",
            local_date=date(2026, 1, 2),
        )
        await session.commit()
        assert claimed_id == original.id
        assert (
            await reclaim_failed_post(
                session,
                guild_id=8,
                discord_message_id=10,
                country_id=country.id,
                channel_id=12,
                body="Ещё раз",
                local_date=date(2026, 1, 3),
            )
            is None
        )
        await session.commit()
        stored = await session.scalar(select(GamePost).where(GamePost.id == original.id))
        assert stored.state == "received"
        assert stored.processing_started_at is None
        assert stored.body == "Обновлённый текст"
        assert stored.channel_id == 12 and stored.local_date == date(2026, 1, 2)
        assert stored.attempts == 1
    await engine.dispose()
