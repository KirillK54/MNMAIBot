import discord
from discord import app_commands
from sqlalchemy import or_, select

from vpi_bot.models import Country, CountryLore, GamePost, Verdict, VerdictOutbox


async def country_choices(
    interaction: discord.Interaction, current: str, *, owner_only: bool = False
) -> list[app_commands.Choice[int]]:
    if interaction.guild_id is None:
        return []
    query = select(Country.id, Country.name).where(
        Country.guild_id == interaction.guild_id,
        Country.is_active.is_(True),
    )
    if owner_only:
        query = query.where(Country.owner_id == interaction.user.id)
    search = current.strip()
    if search:
        conditions = [Country.name.ilike(f"%{search[:80]}%")]
        if search.isdigit():
            conditions.append(Country.id == int(search))
        query = query.where(or_(*conditions))
    query = query.order_by(Country.name).limit(25)
    async with interaction.client.db() as session:
        rows = (await session.execute(query)).all()
    return [
        app_commands.Choice(name=f"{name} · ID {country_id}"[:100], value=country_id)
        for country_id, name in rows
    ]


async def verdict_choices(
    interaction: discord.Interaction,
    current: str,
    *,
    states: tuple[str, ...] = ("pending",),
) -> list[app_commands.Choice[int]]:
    if interaction.guild_id is None:
        return []
    query = (
        select(Verdict.id, Verdict.title, Country.name)
        .join(GamePost, GamePost.id == Verdict.post_id)
        .join(Country, Country.id == GamePost.country_id)
        .where(GamePost.guild_id == interaction.guild_id, Verdict.state.in_(states))
    )
    search = current.strip()
    if search:
        conditions = [Verdict.title.ilike(f"%{search[:80]}%")]
        if search.isdigit():
            conditions.append(Verdict.id == int(search))
        query = query.where(or_(*conditions))
    query = query.order_by(Verdict.created_at.desc()).limit(25)
    async with interaction.client.db() as session:
        rows = (await session.execute(query)).all()
    return [
        app_commands.Choice(name=f"#{verdict_id} · {country} · {title}"[:100], value=verdict_id)
        for verdict_id, title, country in rows
    ]


async def failed_post_choices(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    if interaction.guild_id is None:
        return []
    query = (
        select(GamePost.discord_message_id, GamePost.body, Country.name)
        .join(Country, Country.id == GamePost.country_id)
        .outerjoin(VerdictOutbox, VerdictOutbox.post_id == GamePost.id)
        .where(
            GamePost.guild_id == interaction.guild_id,
            or_(GamePost.state == "failed", VerdictOutbox.status == "dead"),
        )
    )
    search = current.strip()
    if search:
        conditions = [GamePost.body.ilike(f"%{search[:60]}%")]
        if search.isdigit():
            conditions.append(GamePost.discord_message_id == int(search))
        query = query.where(or_(*conditions))
    query = query.order_by(GamePost.created_at.desc()).limit(25)
    async with interaction.client.db() as session:
        rows = (await session.execute(query)).all()
    return [
        app_commands.Choice(
            name=f"{country} · {body[:55].replace(chr(10), ' ')} · ID {message_id}"[:100],
            value=str(message_id),
        )
        for message_id, body, country in rows
    ]


async def incoming_offer_choices(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[int]]:
    if interaction.guild_id is None:
        return []
    from sqlalchemy.orm import aliased

    from vpi_bot.models import TradeOffer

    sender, receiver = aliased(Country), aliased(Country)
    query = (
        select(TradeOffer.id, sender.name, receiver.name, TradeOffer.money, TradeOffer.resources)
        .join(sender, sender.id == TradeOffer.from_country_id)
        .join(receiver, receiver.id == TradeOffer.to_country_id)
        .where(
            TradeOffer.guild_id == interaction.guild_id,
            TradeOffer.status == "pending",
            receiver.owner_id == interaction.user.id,
        )
    )
    search = current.strip()
    if search:
        conditions = [sender.name.ilike(f"%{search[:60]}%")]
        if search.isdigit():
            conditions.append(TradeOffer.id == int(search))
        query = query.where(or_(*conditions))
    query = query.order_by(TradeOffer.created_at).limit(25)
    async with interaction.client.db() as session:
        rows = (await session.execute(query)).all()
    choices = []
    for offer_id, sender_name, receiver_name, money, resources in rows:
        summary = f"деньги {money:g}" if money else ", ".join(resources or {}) or "ресурсы"
        choices.append(
            app_commands.Choice(
                name=f"#{offer_id} · {sender_name} → {receiver_name} · {summary}"[:100],
                value=offer_id,
            )
        )
    return choices


async def lore_choices(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[int]]:
    if interaction.guild_id is None:
        return []
    query = (
        select(CountryLore.id, Country.name, CountryLore.title)
        .join(Country, Country.id == CountryLore.country_id)
        .where(Country.guild_id == interaction.guild_id)
    )
    search = current.strip()
    if search:
        conditions = [
            Country.name.ilike(f"%{search[:60]}%"),
            CountryLore.title.ilike(f"%{search[:60]}%"),
        ]
        if search.isdigit():
            conditions.append(CountryLore.id == int(search))
        query = query.where(or_(*conditions))
    query = query.order_by(CountryLore.importance.desc(), CountryLore.id.desc()).limit(25)
    async with interaction.client.db() as session:
        rows = (await session.execute(query)).all()
    return [
        app_commands.Choice(name=f"#{lore_id} · {country} · {title}"[:100], value=lore_id)
        for lore_id, country, title in rows
    ]
