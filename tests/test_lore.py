import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from vpi_bot.lore import refresh_event_summary
from vpi_bot.models import Base, Country, HistoricalEvent, LoreSummary


@pytest.mark.asyncio
async def test_event_timeline_summary_compacts_in_batches(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'lore.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        country = Country(
            guild_id=31,
            owner_id=32,
            name="Держава",
            government="Республика",
            religion="Нет",
            currency="талер",
        )
        session.add(country)
        await session.flush()
        session.add_all(
            [
                HistoricalEvent(
                    country_id=country.id,
                    title=f"Событие {i}",
                    content=f"Описание события номер {i}.",
                    importance=5,
                )
                for i in range(10)
            ]
        )
        await session.flush()
        assert await refresh_event_summary(session, country.id)
        summary = await session.scalar(
            select(LoreSummary).where(LoreSummary.country_id == country.id)
        )
        assert summary is not None
        assert "Событие 9" in summary.content
        assert summary.through_event_id is not None
        await session.commit()
        assert not await refresh_event_summary(session, country.id)
    await engine.dispose()
