import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from vpi_bot.models import Base, Epoch, ResourceDefinition
from vpi_bot.seed import BASE_RESOURCES, seed_guild_defaults


def test_curated_resource_catalog_has_twenty_five_unique_entries():
    names = [name for name, _, _ in BASE_RESOURCES]
    assert len(BASE_RESOURCES) == 25
    assert len(names) == len(set(names))


@pytest.mark.asyncio
async def test_initial_world_defaults_seed_once_without_overwriting_custom_data(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'seed.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        await seed_guild_defaults(session, 44)
        custom = await session.scalar(
            select(ResourceDefinition).where(
                ResourceDefinition.guild_id == 44, ResourceDefinition.name == "Уголь"
            )
        )
        custom.description = "Переопределено администратором"
        await session.commit()
        await seed_guild_defaults(session, 44)
        resources = (
            await session.scalars(
                select(ResourceDefinition).where(ResourceDefinition.guild_id == 44)
            )
        ).all()
        epochs = (await session.scalars(select(Epoch).where(Epoch.guild_id == 44))).all()
        assert len(resources) == len(BASE_RESOURCES)
        assert len(epochs) == 9
        assert custom.description == "Переопределено администратором"
    await engine.dispose()
