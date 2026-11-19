from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from vpi_bot.ai.schemas import JudgeResult
from vpi_bot.judge import judge_post
from vpi_bot.models import (
    Base,
    Country,
    EconomicStats,
    GamePost,
    GuildConfig,
    HistoricalEvent,
    Verdict,
)


class FakeProvider:
    def __init__(self, result: JudgeResult):
        self.result = result

    async def judge(self, system_prompt: str, context: dict, post: str) -> JudgeResult:
        assert "untrusted_post_note" in context
        assert "недоверенные игровые данные" in system_prompt
        assert post == "Провести программу ремонта железных дорог."
        return self.result


@pytest.mark.asyncio
async def test_judge_output_is_verified_and_stored_as_event(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'judge.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        country = Country(
            guild_id=91,
            owner_id=92,
            name="Республика",
            government="Республика",
            religion="Светская",
            currency="рубль",
        )
        country.economy = EconomicStats(treasury=Decimal(10000), gdp=Decimal(50000))
        session.add(country)
        await session.flush()
        post = GamePost(
            guild_id=91,
            discord_message_id=9192,
            channel_id=9193,
            author_id=92,
            country_id=country.id,
            body="Провести программу ремонта железных дорог.",
            local_date=date(2026, 1, 2),
        )
        config = GuildConfig(
            guild_id=91, epoch_name="Индустриальная эпоха", verdict_mode="automatic"
        )
        session.add_all([post, config])
        await session.flush()
        provider = FakeProvider(
            JudgeResult(
                status="partial_success",
                title="Ремонт путей",
                description="Начаты подготовительные работы.",
                reasoning="Реализуемо поэтапно.",
                positives=["Логистика улучшится"],
                changes={"metrics": {"treasury": -500}, "resources": {}},
                duration_turns=2,
            )
        )
        result, verdict = await judge_post(session, provider, post, country, config)
        await session.commit()
        stats = await session.get(EconomicStats, country.id)
        events = (
            await session.scalars(
                select(HistoricalEvent).where(HistoricalEvent.country_id == country.id)
            )
        ).all()
        stored = await session.scalar(select(Verdict).where(Verdict.post_id == post.id))
        assert result.status == "partial_success"
        assert verdict.state == "applied"
        assert stats.treasury == Decimal(9500)
        assert post.state == "judged"
        assert stored.applied_changes["metrics"]["treasury"] == -500
        assert len(events) == 1
    await engine.dispose()
