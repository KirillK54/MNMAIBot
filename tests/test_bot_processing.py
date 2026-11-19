from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from vpi_bot.ai.schemas import JudgeResult
from vpi_bot.bot import VPIBot
from vpi_bot.models import (
    Base,
    Country,
    EconomicStats,
    GamePost,
    GuildConfig,
    Verdict,
    VerdictOutbox,
)


class FakeAI:
    def __init__(self, changes):
        self.changes = changes

    async def judge(self, system_prompt, context, post):
        assert "untrusted_post_note" in context
        return JudgeResult(
            is_game_action=True,
            status="success",
            title="Постановление",
            description="Программа одобрена.",
            changes=self.changes,
        )


async def make_bot_fixture(tmp_path, changes):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'bot-processing.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        country = Country(
            guild_id=500,
            owner_id=600,
            name="Тестландия",
            government="Республика",
            religion="Нет",
            currency="рубль",
            channel_id=700,
        )
        country.economy = EconomicStats(treasury=Decimal(1000), gdp=Decimal(5000))
        config = GuildConfig(guild_id=500, timezone="Europe/Saratov", verdict_mode="automatic")
        session.add_all([country, config])
        await session.commit()

    calls = []

    async def guild_config(guild_id):
        assert guild_id == 500
        async with factory() as session:
            return await session.get(GuildConfig, guild_id)

    async def flush_outbox():
        calls.append("outbox")

    fake_bot = SimpleNamespace(
        db=factory,
        guild_config=guild_config,
        ai=FakeAI(changes),
        settings=SimpleNamespace(authorized_guild_id=None),
        user=SimpleNamespace(id=999),
        flush_outbox=flush_outbox,
    )

    class FakeMessage:
        id = 800
        content = "Начать программу промышленной модернизации."
        guild = SimpleNamespace(id=500)
        channel = SimpleNamespace(id=700)
        author = SimpleNamespace(id=600, bot=False)

        def __init__(self):
            self.reactions = []
            self.reply_calls = []

        async def add_reaction(self, emoji):
            self.reactions.append(("add", emoji))

        async def remove_reaction(self, emoji, user):
            self.reactions.append(("remove", emoji, user.id))

        async def reply(self, *args, **kwargs):
            self.reply_calls.append((args, kwargs))
            raise AssertionError("Verdict replies must be dispatched by the durable outbox")

    return engine, factory, fake_bot, FakeMessage(), calls


@pytest.mark.asyncio
async def test_message_processing_commits_verdict_and_reply_outbox_together(tmp_path):
    engine, factory, fake_bot, message, calls = await make_bot_fixture(
        tmp_path, {"metrics": {"stability": 1}, "resources": {}}
    )
    await VPIBot._process_message(fake_bot, message)

    async with factory() as session:
        post = await session.scalar(
            select(GamePost).where(GamePost.discord_message_id == message.id)
        )
        verdict = await session.scalar(select(Verdict).where(Verdict.post_id == post.id))
        outbox = await session.scalar(select(VerdictOutbox).where(VerdictOutbox.post_id == post.id))
        stats = await session.get(EconomicStats, post.country_id)
        assert post.state == "judged"
        assert verdict.state == "applied"
        assert stats.stability == Decimal("71.000")
        assert outbox.status == "pending"
        assert outbox.source_message_id == message.id
        assert outbox.channel_id == message.channel.id
        footer = outbox.embed_payload["footer"]["text"]
        assert "пост #800" in footer
        assert f"вердикт #{verdict.id}" in footer
        assert "осталось постов: 2" in footer
        assert verdict.message_id is None
    assert calls == ["outbox"]
    assert message.reply_calls == []
    assert ("add", "✅") in message.reactions
    await engine.dispose()


@pytest.mark.asyncio
async def test_engine_rejected_verdict_is_also_queued_atomically(tmp_path):
    engine, factory, fake_bot, message, calls = await make_bot_fixture(
        tmp_path, {"metrics": {"treasury": -10000}, "resources": {}}
    )
    await VPIBot._process_message(fake_bot, message)

    async with factory() as session:
        post = await session.scalar(
            select(GamePost).where(GamePost.discord_message_id == message.id)
        )
        verdict = await session.scalar(select(Verdict).where(Verdict.post_id == post.id))
        outbox = await session.scalar(select(VerdictOutbox).where(VerdictOutbox.post_id == post.id))
        stats = await session.get(EconomicStats, post.country_id)
        assert post.state == "engine_rejected"
        assert verdict.state == "engine_rejected"
        assert stats.treasury == Decimal(1000)
        assert outbox.status == "pending"
        assert "Состояние" in [field["name"] for field in outbox.embed_payload["fields"]]
        assert "отклонил" in outbox.embed_payload["description"].casefold()
    assert calls == ["outbox"]
    assert message.reply_calls == []
    assert ("add", "⚖️") in message.reactions
    await engine.dispose()
