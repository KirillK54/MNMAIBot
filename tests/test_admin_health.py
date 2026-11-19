from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from vpi_bot.bot import VPIBot
from vpi_bot.commands import install_commands
from vpi_bot.config import Settings
from vpi_bot.models import (
    Base,
    Country,
    EconomicStats,
    GamePost,
    GuildConfig,
    Verdict,
    VerdictOutbox,
)


class FakeResponse:
    def __init__(self):
        self.kwargs = None

    async def send_message(self, content=None, **kwargs):
        if content is not None:
            kwargs["content"] = content
        self.kwargs = kwargs


@pytest.mark.asyncio
async def test_admin_panel_reports_database_ai_and_recovery_counts(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'admin-health.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    settings = Settings(ai_api_key="secret-must-never-be-shown", authorized_guild_id=77)
    bot = VPIBot(settings, factory)
    install_commands(bot)
    async with factory() as session:
        country = Country(
            guild_id=77,
            owner_id=88,
            name="Тест",
            government="Республика",
            religion="Нет",
            currency="рубль",
        )
        country.economy = EconomicStats(treasury=Decimal(1000))
        config = GuildConfig(guild_id=77, timezone="Europe/Saratov", verdict_mode="automatic")
        session.add_all([country, config])
        await session.flush()
        post = GamePost(
            guild_id=77,
            discord_message_id=99,
            channel_id=100,
            author_id=88,
            country_id=country.id,
            body="Пост",
            local_date=datetime.now(UTC).date(),
            state="failed",
        )
        session.add(post)
        await session.flush()
        verdict = Verdict(
            post_id=post.id,
            status="success",
            title="Ответ",
            description="Текст",
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
                guild_id=77,
                channel_id=100,
                source_message_id=99,
                embed_payload={"title": "Ответ"},
                status="pending",
            )
        )
        await session.commit()

    response = FakeResponse()
    interaction = SimpleNamespace(guild_id=77, response=response)
    await bot.tree.get_command("admin").callback(interaction)
    embed = response.kwargs["embed"]
    status = next(field.value for field in embed.fields if field.name == "Состояние сервисов")
    assert "База данных: доступна" in status
    assert "Ключ ИИ: настроен" in status
    assert "Недоставлено вердиктов: 1" in status
    assert "Постов для повтора: 1" in status
    assert "secret-must-never-be-shown" not in str(embed.to_dict())
    assert response.kwargs["ephemeral"] is True
    await bot.close()
    await engine.dispose()


@pytest.mark.asyncio
async def test_admin_panel_can_report_database_failure_to_discord_admin():
    bot = VPIBot(Settings(ai_api_key="configured-secret"), session_factory=object())
    install_commands(bot)

    async def database_down(guild_id):
        raise OperationalError("SELECT 1", {}, RuntimeError("database offline"))

    bot.guild_config = database_down
    response = FakeResponse()
    interaction = SimpleNamespace(
        guild_id=77, permissions=SimpleNamespace(administrator=True), response=response
    )
    await bot.tree.get_command("admin").callback(interaction)
    embed = response.kwargs["embed"]
    assert any(
        field.name == "База данных" and field.value == "недоступна" for field in embed.fields
    )
    assert "Настройки сервера" in embed.description
    assert "configured-secret" not in str(embed.to_dict())
    assert response.kwargs["ephemeral"] is True
    await bot.close()
