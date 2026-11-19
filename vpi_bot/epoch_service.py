import json
import logging

import discord
from sqlalchemy import func, select

from vpi_bot.models import (
    AuditLog,
    Country,
    CountryResource,
    EconomicStats,
    Epoch,
    GuildConfig,
    MilitaryStats,
)
from vpi_bot.ui.cards import country_overview_embed

log = logging.getLogger(__name__)


async def apply_epoch_selection(
    bot, guild_id: int, actor_id: int, epoch_id: int
) -> tuple[str, bool]:
    async with bot.db() as session:
        config = await session.scalar(
            select(GuildConfig).where(GuildConfig.guild_id == guild_id).with_for_update()
        )
        epoch = await session.scalar(
            select(Epoch).where(Epoch.guild_id == guild_id, Epoch.id == epoch_id)
        )
        if config is None or epoch is None:
            raise ValueError("Эпоха или конфигурация сервера больше недоступны.")
        old_name = config.epoch_name
        config.epoch_name = epoch.name
        session.add(
            AuditLog(
                guild_id=guild_id,
                actor_id=actor_id,
                action="epoch_set",
                target_type="guild",
                target_id=str(guild_id),
                old_value=json.dumps({"name": old_name}, ensure_ascii=False),
                new_value=json.dumps(
                    {
                        "name": epoch.name,
                        "rules": epoch.rules,
                        "technologies": epoch.technologies,
                        "starting_stats": epoch.starting_stats,
                    },
                    ensure_ascii=False,
                ),
            )
        )
        name, has_template = epoch.name, bool(epoch.starting_stats)
        await session.commit()
    return name, has_template


async def refresh_country_epoch_cards(bot, guild_id: int) -> tuple[int, int]:
    async with bot.db() as session:
        config = await session.get(GuildConfig, guild_id)
        epoch = (
            await session.scalar(
                select(Epoch).where(Epoch.guild_id == guild_id, Epoch.name == config.epoch_name)
            )
            if config
            else None
        )
        countries = (
            await session.scalars(
                select(Country).where(
                    Country.guild_id == guild_id,
                    Country.is_active.is_(True),
                    Country.info_message_id.is_not(None),
                )
            )
        ).all()
        cards = []
        for country in countries:
            stats = await session.get(EconomicStats, country.id)
            army = await session.get(MilitaryStats, country.id)
            resource_count = await session.scalar(
                select(func.count(CountryResource.resource_id)).where(
                    CountryResource.country_id == country.id
                )
            )
            cards.append((country, stats, army, resource_count or 0))
    if epoch is None:
        return 0, len(cards)
    updated = failed = 0
    for country, stats, army, resource_count in cards:
        channel = bot.get_channel(country.channel_id) if country.channel_id else None
        if channel is None and country.channel_id:
            try:
                channel = await bot.fetch_channel(country.channel_id)
            except discord.HTTPException:
                channel = None
        if channel is None:
            failed += 1
            continue
        try:
            message = await channel.fetch_message(country.info_message_id)
            embed = country_overview_embed(
                country, stats, army, resource_count=resource_count, epoch=epoch
            )
            embed.add_field(name="Глава государства", value=f"<@{country.owner_id}>", inline=True)
            await message.edit(embed=embed, allowed_mentions=discord.AllowedMentions.none())
            updated += 1
        except discord.HTTPException:
            failed += 1
            log.exception("Failed to refresh epoch on country card %s", country.id)
    return updated, failed
