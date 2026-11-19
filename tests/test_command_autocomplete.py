from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import discord
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from vpi_bot.commands.autocomplete import (
    country_choices,
    failed_post_choices,
    incoming_offer_choices,
    verdict_choices,
)
from vpi_bot.commands.moderation import VerdictReviewView
from vpi_bot.models import Base, Country, GamePost, TradeOffer, Verdict


@pytest.mark.asyncio
async def test_country_verdict_post_and_trade_autocomplete_only_shows_relevant_rows(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'autocomplete.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        alpha = Country(
            guild_id=10,
            owner_id=100,
            name="Альфа",
            government="Республика",
            religion="Нет",
            currency="альф",
        )
        beta = Country(
            guild_id=10,
            owner_id=200,
            name="Бета",
            government="Монархия",
            religion="Нет",
            currency="бет",
        )
        archived = Country(
            guild_id=10,
            owner_id=100,
            name="Архив",
            government="Республика",
            religion="Нет",
            currency="арх",
            is_active=False,
        )
        session.add_all([alpha, beta, archived])
        await session.flush()
        post = GamePost(
            guild_id=10,
            discord_message_id=555,
            channel_id=700,
            author_id=100,
            country_id=alpha.id,
            body="Проверяемый пост",
            local_date=date(2026, 11, 19),
            state="failed",
        )
        session.add(post)
        await session.flush()
        session.add(
            Verdict(
                post_id=post.id,
                status="success",
                title="Торговая реформа",
                description="Одобрена частично.",
                proposed_changes={},
                consequences={},
                state="pending",
            )
        )
        session.add(
            TradeOffer(
                guild_id=10,
                from_country_id=beta.id,
                to_country_id=alpha.id,
                money=Decimal("25.00"),
                resources={"Железо": 2},
                status="pending",
            )
        )
        await session.commit()
        alpha_id = alpha.id

    bot = SimpleNamespace(db=factory)
    interaction = SimpleNamespace(
        guild_id=10,
        user=SimpleNamespace(id=100),
        client=bot,
    )
    all_countries = await country_choices(interaction, "")
    owned_countries = await country_choices(interaction, "", owner_only=True)
    verdicts = await verdict_choices(interaction, "")
    failed_posts = await failed_post_choices(interaction, "")
    offers = await incoming_offer_choices(interaction, "")

    assert {choice.value for choice in all_countries} == {1, 2}
    assert [choice.value for choice in owned_countries] == [alpha_id]
    assert len(verdicts) == 1 and verdicts[0].name.startswith("#1")
    assert len(failed_posts) == 1 and failed_posts[0].value == "555"
    assert len(offers) == 1 and "Бета" in offers[0].name
    await engine.dispose()


def test_moderation_review_has_select_and_disabled_action_buttons_until_selection():
    view = VerdictReviewView(
        SimpleNamespace(),
        guild_id=10,
        owner_id=100,
        rows=[(7, "Реформа", "Альфа", 100)],
    )
    selector = next(item for item in view.children if isinstance(item, discord.ui.Select))
    buttons = [item for item in view.children if isinstance(item, discord.ui.Button)]
    assert selector.options[0].value == "7"
    assert len(buttons) == 2
    assert all(button.disabled for button in buttons)
