from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from vpi_bot.game_engine import ChangeRejected, apply_changes, reverse_changes
from vpi_bot.industry import run_facility_cycle
from vpi_bot.models import (
    Base,
    ConstructionProject,
    Country,
    CountryResource,
    CountryTechnology,
    EconomicStats,
    GamePost,
    IndustryFacility,
    MilitaryStats,
    ResearchProject,
    ResourceDefinition,
    Verdict,
)


@pytest.fixture
async def db_session(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        country = Country(
            guild_id=101,
            owner_id=202,
            name="Тестландия",
            government="Республика",
            religion="Нет",
            currency="талер",
            channel_id=303,
        )
        country.economy = EconomicStats(treasury=Decimal(1000), gdp=Decimal(5000))
        resource = ResourceDefinition(guild_id=101, name="Железо", unit="т")
        steel = ResourceDefinition(guild_id=101, name="Сталь", unit="т")
        session.add_all([country, resource, steel])
        await session.flush()
        session.add(
            CountryResource(country_id=country.id, resource_id=resource.id, quantity=Decimal(10))
        )
        post = GamePost(
            guild_id=101,
            discord_message_id=999,
            channel_id=303,
            author_id=202,
            country_id=country.id,
            body="test",
            local_date=__import__("datetime").date.today(),
        )
        session.add(post)
        await session.flush()
        verdict = Verdict(
            post_id=post.id,
            status="success",
            title="Тест",
            description="Тест",
            proposed_changes={
                "metrics": {},
                "resources": {},
                "military": {},
                "projects": [],
                "research": [],
            },
            state="pending",
        )
        session.add(verdict)
        await session.flush()
        yield session, country, verdict, resource
    await engine.dispose()


@pytest.mark.asyncio
async def test_valid_changes_apply_and_reverse(db_session):
    session, country, verdict, resource = db_session
    await apply_changes(
        session,
        country,
        verdict,
        {
            "metrics": {"treasury": -120, "growth": 1.5},
            "resources": {"Железо": -3},
        },
    )
    stats = await session.get(EconomicStats, country.id)
    inventory = await session.get(CountryResource, (country.id, resource.id))
    assert stats.treasury == Decimal(880)
    assert stats.growth == Decimal("3.5")
    assert inventory.quantity == Decimal(7)
    await reverse_changes(session, verdict)
    assert stats.treasury == Decimal(1000)
    assert stats.growth == Decimal("2.0")
    assert inventory.quantity == Decimal(10)


@pytest.mark.asyncio
async def test_negative_growth_is_valid_but_fractional_industry_level_is_not(db_session):
    session, country, verdict, _ = db_session
    await apply_changes(
        session,
        country,
        verdict,
        {
            "metrics": {"growth": -3},
            "resources": {},
            "military": {},
            "projects": [],
        },
    )
    stats = await session.get(EconomicStats, country.id)
    assert stats.growth == Decimal("-1.0")
    await reverse_changes(session, verdict)
    assert stats.growth == Decimal("2.0")
    with pytest.raises(ChangeRejected, match="целым числом"):
        await apply_changes(
            session,
            country,
            verdict,
            {
                "metrics": {"industry_level": 1.5},
                "resources": {},
            },
        )


@pytest.mark.asyncio
async def test_military_changes_apply_and_reverse(db_session):
    session, country, verdict, _ = db_session
    army = MilitaryStats(
        country_id=country.id,
        personnel=100,
        readiness=Decimal(50),
        units={"infantry": 20},
        equipment={"rifle": 5},
    )
    session.add(army)
    await session.flush()
    await apply_changes(
        session,
        country,
        verdict,
        {
            "metrics": {},
            "resources": {},
            "military": {
                "metrics": {"personnel": -10, "readiness": 5, "budget": 20},
                "units": {"infantry": 5},
                "equipment": {"rifle": 2},
                "ammunition": {"shell": 10},
            },
        },
    )
    assert army.personnel == 90
    assert army.budget == Decimal(20)
    assert (await session.get(EconomicStats, country.id)).treasury == Decimal(980)
    assert army.readiness == Decimal(55)
    assert army.units["infantry"] == 25
    assert army.equipment["rifle"] == 7
    assert army.ammunition["shell"] == 10
    await reverse_changes(session, verdict)
    assert army.personnel == 100
    assert army.budget == Decimal(0)
    assert (await session.get(EconomicStats, country.id)).treasury == Decimal(1000)
    assert army.readiness == Decimal(50)
    assert army.units["infantry"] == 20
    assert army.equipment["rifle"] == 5
    assert army.ammunition["shell"] == 0


@pytest.mark.asyncio
async def test_project_reserves_money_resources_and_can_be_reversed(db_session):
    session, country, verdict, iron = db_session
    applied = await apply_changes(
        session,
        country,
        verdict,
        {
            "metrics": {},
            "resources": {},
            "military": {},
            "projects": [
                {
                    "name": "Сталелитейный завод",
                    "facility_type": "Металлургия",
                    "cost": 200,
                    "resource_costs": {"Железо": 2},
                    "production_per_tick": {"Сталь": 3},
                    "input_per_tick": {"Железо": 1},
                    "operating_cost": 10,
                    "interval_hours": 4,
                    "duration_turns": 3,
                    "required_technology": None,
                }
            ],
        },
    )
    project = await session.scalar(
        select(ConstructionProject).where(ConstructionProject.source_verdict_id == verdict.id)
    )
    stats = await session.get(EconomicStats, country.id)
    inventory = await session.get(CountryResource, (country.id, iron.id))
    assert project.status == "building" and project.completed_turns == 0
    assert stats.treasury == Decimal(800)
    assert inventory.quantity == Decimal(8)
    assert applied["projects"][0]["name"] == "Сталелитейный завод"
    await reverse_changes(session, verdict)
    assert (
        await session.scalar(
            select(ConstructionProject.id).where(
                ConstructionProject.source_verdict_id == verdict.id
            )
        )
        is None
    )
    assert stats.treasury == Decimal(1000)
    assert inventory.quantity == Decimal(10)


@pytest.mark.asyncio
async def test_construction_progress_completes_and_launches_production(db_session):
    session, country, verdict, _ = db_session
    await apply_changes(
        session,
        country,
        verdict,
        {
            "metrics": {},
            "resources": {},
            "military": {},
            "projects": [
                {
                    "name": "Электростанция",
                    "facility_type": "Энергетика",
                    "cost": 100,
                    "resource_costs": {},
                    "production_per_tick": {"Сталь": 1},
                    "input_per_tick": {},
                    "operating_cost": 0,
                    "interval_hours": 4,
                    "duration_turns": 1,
                    "required_technology": None,
                }
            ],
        },
    )
    second_post = GamePost(
        guild_id=101,
        discord_message_id=1000,
        channel_id=303,
        author_id=202,
        country_id=country.id,
        body="next turn",
        local_date=verdict.created_at.date(),
    )
    session.add(second_post)
    await session.flush()
    second = Verdict(
        post_id=second_post.id,
        status="success",
        title="Следующий ход",
        description="Тест",
        proposed_changes={},
        applied_changes={},
        state="applied",
    )
    session.add(second)
    await session.flush()
    second.applied_changes = await apply_changes(
        session,
        country,
        second,
        {
            "metrics": {},
            "resources": {},
            "military": {},
            "projects": [],
        },
    )
    facility = await session.scalar(
        select(IndustryFacility).where(IndustryFacility.source_project_id.is_not(None))
    )
    assert facility is not None and facility.name == "Электростанция"
    report = await run_facility_cycle(session, facility)
    steel = await session.scalar(
        select(ResourceDefinition).where(
            ResourceDefinition.guild_id == 101, ResourceDefinition.name == "Сталь"
        )
    )
    steel_stock = await session.get(CountryResource, (country.id, steel.id))
    assert report is not None and steel_stock.quantity == Decimal(1)
    with pytest.raises(ChangeRejected, match="завершил строительный проект"):
        await reverse_changes(session, second)


@pytest.mark.asyncio
async def test_research_project_starts_without_cost_or_prerequisites(db_session):
    session, country, verdict, iron = db_session
    await apply_changes(
        session,
        country,
        verdict,
        {
            "metrics": {},
            "resources": {},
            "military": {},
            "research": [
                {
                    "technology_name": "Радиосвязь",
                    "description": "Освоение радиопередачи.",
                    "duration_turns": 3,
                }
            ],
        },
    )
    project = await session.scalar(
        select(ResearchProject).where(ResearchProject.source_verdict_id == verdict.id)
    )
    stats = await session.get(EconomicStats, country.id)
    inventory = await session.get(CountryResource, (country.id, iron.id))
    assert project.status == "researching" and project.completed_turns == 0
    assert stats.treasury == Decimal(1000) and inventory.quantity == Decimal(10)
    await reverse_changes(session, verdict)
    assert (
        await session.scalar(
            select(ResearchProject.id).where(ResearchProject.source_verdict_id == verdict.id)
        )
        is None
    )
    assert stats.treasury == Decimal(1000) and inventory.quantity == Decimal(10)


@pytest.mark.asyncio
async def test_research_completes_after_accepted_actions_and_blocks_unsafe_rollback(db_session):
    session, country, verdict, _ = db_session
    await apply_changes(
        session,
        country,
        verdict,
        {
            "metrics": {},
            "resources": {},
            "military": {},
            "research": [
                {
                    "technology_name": "Радиосвязь",
                    "description": "Научена передача радиосигнала.",
                    "duration_turns": 1,
                }
            ],
        },
    )
    next_post = GamePost(
        guild_id=101,
        discord_message_id=1001,
        channel_id=303,
        author_id=202,
        country_id=country.id,
        body="next successful action",
        local_date=verdict.created_at.date(),
    )
    session.add(next_post)
    await session.flush()
    next_verdict = Verdict(
        post_id=next_post.id,
        status="success",
        title="Следующий ход",
        description="Тест",
        proposed_changes={},
        applied_changes={},
        state="applied",
    )
    session.add(next_verdict)
    await session.flush()
    next_verdict.applied_changes = await apply_changes(
        session,
        country,
        next_verdict,
        {
            "metrics": {},
            "resources": {},
            "military": {},
            "research": [],
        },
    )
    technology = await session.scalar(
        select(CountryTechnology).where(
            CountryTechnology.country_id == country.id, CountryTechnology.name == "Радиосвязь"
        )
    )
    assert technology.level == 1 and technology.status == "researched"
    with pytest.raises(ChangeRejected, match="завершил исследование"):
        await reverse_changes(session, next_verdict)


@pytest.mark.asyncio
async def test_negative_treasury_rejected(db_session):
    session, country, verdict, _ = db_session
    with pytest.raises(ChangeRejected, match="Недостаточно"):
        await apply_changes(
            session, country, verdict, {"metrics": {"treasury": -1001}, "resources": {}}
        )


@pytest.mark.asyncio
async def test_unknown_metric_or_resource_rejected(db_session):
    session, country, verdict, _ = db_session
    with pytest.raises(ChangeRejected, match="Неизвестный"):
        await apply_changes(
            session, country, verdict, {"metrics": {"god_mode": 1}, "resources": {}}
        )
    with pytest.raises(ChangeRejected, match="не зарегистрирован"):
        await apply_changes(
            session, country, verdict, {"metrics": {}, "resources": {"adamantium": 1}}
        )


@pytest.mark.asyncio
async def test_no_negative_inventory(db_session):
    session, country, verdict, _ = db_session
    with pytest.raises(ChangeRejected, match="Недостаточно ресурса"):
        await apply_changes(
            session, country, verdict, {"metrics": {}, "resources": {"Железо": -11}}
        )
