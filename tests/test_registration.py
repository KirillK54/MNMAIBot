import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from vpi_bot.models import Base, Country
from vpi_bot.registration import registration_allowed


def country(owner_id: int, name: str, *, active: bool = True) -> Country:
    return Country(
        guild_id=5,
        owner_id=owner_id,
        name=name,
        government="Республика",
        religion="Нет",
        currency="рубль",
        is_active=active,
    )


@pytest.mark.asyncio
async def test_registration_policy_allows_optional_multiple_countries(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'registration.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        archived = country(12, "Первое", active=False)
        session.add(archived)
        await session.flush()
        assert await registration_allowed(session, 5, 12, allow_multiple=False)
        active = country(12, "Второе")
        session.add(active)
        await session.flush()
        assert not await registration_allowed(session, 5, 12, allow_multiple=False)
        assert await registration_allowed(session, 5, 12, allow_multiple=True)
        session.add(country(12, "Третье"))
        await session.commit()
    await engine.dispose()
