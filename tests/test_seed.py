import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from vpi_bot.commands.world import _parse_epoch_starting_stats
from vpi_bot.models import Base, Epoch, ResourceDefinition
from vpi_bot.registration import epoch_starting_values
from vpi_bot.seed import BASE_RESOURCES, DEFAULT_EPOCHS, seed_guild_defaults


def test_curated_resource_catalog_has_twenty_five_unique_entries():
    names = [name for name, _, _ in BASE_RESOURCES]
    assert len(BASE_RESOURCES) == 25
    assert len(names) == len(set(names))


def test_predefined_eras_have_distinct_rules_technologies_and_starting_state():
    presets = {preset.name: preset for preset in DEFAULT_EPOCHS}
    assert "Каменный век" in presets
    assert "Бронзовый век" in presets
    assert all(
        preset.rules and preset.technologies and preset.starting_stats
        for preset in presets.values()
    )
    resource_names = {name for name, _, _ in BASE_RESOURCES}
    assert all(
        set(preset.starting_stats["resources"]) <= resource_names for preset in presets.values()
    )

    stone_economy, stone_military, stone_resources = epoch_starting_values(
        Epoch(starting_stats=presets["Каменный век"].starting_stats)
    )
    industrial_economy, industrial_military, industrial_resources = epoch_starting_values(
        Epoch(starting_stats=presets["Индустриальная эпоха"].starting_stats)
    )
    assert stone_economy.treasury < industrial_economy.treasury
    assert stone_economy.industry_level < industrial_economy.industry_level
    assert stone_military["technology_level"] < industrial_military["technology_level"]
    assert "Камень" in stone_resources and "Машины" in industrial_resources


def test_custom_era_starting_stats_json_is_validated_and_normalized():
    parsed = _parse_epoch_starting_stats(
        '{"economy":{"treasury":1000},"military":{"personnel":300},"resources":{"Зерно":500}}'
    )
    assert parsed == {
        "economy": {"treasury": 1000},
        "military": {"personnel": 300},
        "resources": {"Зерно": 500},
    }
    with pytest.raises(ValueError):
        _parse_epoch_starting_stats('{"economy":{"treasury":-1}}')
    with pytest.raises(ValueError):
        _parse_epoch_starting_stats('{"resources":{"Уран":NaN}}')


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
        industrial = await session.scalar(
            select(Epoch).where(Epoch.guild_id == 44, Epoch.name == "Индустриальная эпоха")
        )
        industrial.rules = "Серверное правило эпохи"
        industrial.starting_stats = {"economy": {"treasury": 42}}
        await session.commit()
        await seed_guild_defaults(session, 44)
        resources = (
            await session.scalars(
                select(ResourceDefinition).where(ResourceDefinition.guild_id == 44)
            )
        ).all()
        epochs = (await session.scalars(select(Epoch).where(Epoch.guild_id == 44))).all()
        assert len(resources) == len(BASE_RESOURCES)
        assert len(epochs) == len(DEFAULT_EPOCHS) == 12
        assert custom.description == "Переопределено администратором"
        assert industrial.rules == "Серверное правило эпохи"
        assert industrial.starting_stats == {"economy": {"treasury": 42}}
    await engine.dispose()
