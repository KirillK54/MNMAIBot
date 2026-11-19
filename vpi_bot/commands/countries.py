import json
import logging

import discord
from discord import app_commands
from sqlalchemy import select

from vpi_bot.commands.autocomplete import country_choices
from vpi_bot.models import Country, GuildConfig
from vpi_bot.security import admin_check

log = logging.getLogger(__name__)


def register(bot) -> None:
    @bot.tree.command(
        name="country-delete", description="Архивировать государство и закрыть его канал"
    )
    @app_commands.describe(
        country_id="ID активного государства из /countries",
        confirm_name="Точное название государства для подтверждения опасного действия",
    )
    @admin_check(bot)
    async def country_delete(interaction: discord.Interaction, country_id: int, confirm_name: str):
        from vpi_bot.models import AuditLog

        async with bot.db() as session:
            country = await session.scalar(
                select(Country)
                .where(
                    Country.id == country_id,
                    Country.guild_id == interaction.guild_id,
                    Country.is_active.is_(True),
                )
                .with_for_update()
            )
            if not country or confirm_name.strip() != country.name:
                await interaction.response.send_message(
                    "Государство не найдено либо подтверждение названия не совпало.", ephemeral=True
                )
                return
            country.is_active = False
            channel_id = country.channel_id
            session.add(
                AuditLog(
                    guild_id=interaction.guild_id,
                    actor_id=interaction.user.id,
                    action="country_archive",
                    target_type="country",
                    target_id=str(country.id),
                    old_value=country.name,
                    new_value="inactive",
                    reason="Архивировано администратором",
                )
            )
            await session.commit()
        if channel_id:
            channel = bot.get_channel(channel_id)
            if channel is None:
                try:
                    channel = await bot.fetch_channel(channel_id)
                except discord.HTTPException:
                    channel = None
            if channel:
                try:
                    await channel.delete(reason=f"Архивировано государство {confirm_name}")
                except discord.HTTPException:
                    await interaction.response.send_message(
                        "Государство архивировано, но приватный канал удалить не удалось; удалите его вручную.",
                        ephemeral=True,
                    )
                    return
        await interaction.response.send_message(
            "Государство архивировано; его игровая история сохранена.", ephemeral=True
        )

    @country_delete.autocomplete("country_id")
    async def delete_country_autocomplete(interaction: discord.Interaction, current: str):
        return await country_choices(interaction, current)

    @bot.tree.command(
        name="country-transfer", description="Передать владение государством другому игроку"
    )
    @app_commands.describe(
        country_id="ID государства из /countries",
        new_owner="Новый владелец — участник этого Discord-сервера",
    )
    @admin_check(bot)
    async def country_transfer(
        interaction: discord.Interaction, country_id: int, new_owner: discord.Member
    ):
        from vpi_bot.models import AuditLog

        if not interaction.guild_id or not await bot.guild_config(interaction.guild_id):
            await interaction.response.send_message("Сервер не настроен.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        async with bot.db() as session:
            country = await session.scalar(
                select(Country)
                .where(Country.id == country_id, Country.guild_id == interaction.guild_id)
                .with_for_update()
            )
            if not country:
                await interaction.followup.send("Государство не найдено.", ephemeral=True)
                return
            config = await session.get(GuildConfig, interaction.guild_id)
            other = None
            if not config or not config.allow_multiple_countries:
                other = await session.scalar(
                    select(Country).where(
                        Country.guild_id == interaction.guild_id,
                        Country.owner_id == new_owner.id,
                        Country.is_active.is_(True),
                        Country.id != country.id,
                    )
                )
            if other:
                await interaction.followup.send(
                    "Новый владелец уже владеет государством.", ephemeral=True
                )
                return
            if new_owner.bot or new_owner.id == country.owner_id:
                await interaction.followup.send(
                    "Выберите другого действующего игрока.", ephemeral=True
                )
                return
            role_id = config.registered_role_id if config else None
            old_owner_id, channel_id, info_id = (
                country.owner_id,
                country.channel_id,
                country.info_message_id,
            )
            country.owner_id = new_owner.id
            session.add(
                AuditLog(
                    guild_id=interaction.guild_id,
                    actor_id=interaction.user.id,
                    action="country_transfer",
                    target_type="country",
                    target_id=str(country.id),
                    old_value=str(old_owner_id),
                    new_value=str(new_owner.id),
                    reason="Передача владения администратором",
                )
            )
            await session.commit()
        try:
            old_member = interaction.guild.get_member(
                old_owner_id
            ) or await interaction.guild.fetch_member(old_owner_id)
            if channel_id:
                channel = bot.get_channel(channel_id)
                if channel is None:
                    channel = await bot.fetch_channel(channel_id)
                await channel.set_permissions(
                    old_member, overwrite=None, reason="Передача государства"
                )
                await channel.set_permissions(
                    new_owner,
                    view_channel=True,
                    send_messages=True,
                    read_message_history=True,
                    reason="Новый владелец государства",
                )
                role = interaction.guild.get_role(role_id) if role_id else None
                if role:
                    await new_owner.add_roles(role, reason="Получено владение игровым государством")
                if info_id:
                    card = await channel.fetch_message(info_id)
                    if card.embeds:
                        embed = card.embeds[0]
                        index = next(
                            (
                                i
                                for i, field in enumerate(embed.fields)
                                if field.name == "Глава государства"
                            ),
                            None,
                        )
                        if index is not None:
                            embed.set_field_at(
                                index,
                                name="Глава государства",
                                value=new_owner.mention,
                                inline=True,
                            )
                            await card.edit(embed=embed)
            await interaction.followup.send(
                f"Государство передано {new_owner.mention}.", ephemeral=True
            )
        except discord.HTTPException:
            log.exception(
                "Ownership moved in DB but private channel permissions need manual review"
            )
            await interaction.followup.send(
                "Владелец изменён в базе, но Discord-права обновить не удалось. Проверьте приватный канал вручную.",
                ephemeral=True,
            )

    @country_transfer.autocomplete("country_id")
    async def transfer_country_autocomplete(interaction: discord.Interaction, current: str):
        return await country_choices(interaction, current)

    @bot.tree.command(name="country-edit", description="Изменить карточку государства")
    @app_commands.describe(
        country_id="ID государства из /countries",
        name="Новое название; пусто — не менять",
        government="Форма правления: Республика или Монархия",
        religion="Новая государственная религия",
        currency="Название национальной валюты",
        description="Краткое описание, показываемое в карточке государства",
    )
    @admin_check(bot)
    async def country_edit(
        interaction: discord.Interaction,
        country_id: int,
        name: str | None = None,
        government: str | None = None,
        religion: str | None = None,
        currency: str | None = None,
        description: str | None = None,
    ):
        from vpi_bot.models import AuditLog

        if not any(
            value is not None for value in (name, government, religion, currency, description)
        ):
            await interaction.response.send_message(
                "Укажите хотя бы одно поле для изменения.", ephemeral=True
            )
            return
        if government is not None and government.casefold() not in {"республика", "монархия"}:
            await interaction.response.send_message(
                "Форма правления: Республика или Монархия.", ephemeral=True
            )
            return
        async with bot.db() as session:
            country = await session.scalar(
                select(Country).where(
                    Country.id == country_id, Country.guild_id == interaction.guild_id
                )
            )
            if not country:
                await interaction.response.send_message("Государство не найдено.", ephemeral=True)
                return
            old = {
                key: getattr(country, key)
                for key in ("name", "government", "religion", "currency", "description")
            }
            for key, value, limit in (
                ("name", name, 100),
                ("government", government, 40),
                ("religion", religion, 120),
                ("currency", currency, 80),
                ("description", description, 2000),
            ):
                if value is not None:
                    setattr(country, key, value.strip()[:limit])
            new = {key: getattr(country, key) for key in old}
            channel_id, info_message_id = country.channel_id, country.info_message_id
            session.add(
                AuditLog(
                    guild_id=interaction.guild_id,
                    actor_id=interaction.user.id,
                    action="country_edit",
                    target_type="country",
                    target_id=str(country.id),
                    old_value=json.dumps(old, ensure_ascii=False),
                    new_value=json.dumps(new, ensure_ascii=False),
                )
            )
            await session.commit()
        if channel_id and info_message_id:
            channel = bot.get_channel(channel_id)
            if channel is None:
                try:
                    channel = await bot.fetch_channel(channel_id)
                except discord.HTTPException:
                    channel = None
            if channel:
                try:
                    card = await channel.fetch_message(info_message_id)
                    if card.embeds:
                        embed = card.embeds[0]
                        embed.title = f"🏛 {new['name']}"
                        embed.description = (
                            new["description"]
                            or "Добро пожаловать в игровое пространство государства."
                        )
                        for field_name, field_value in (
                            ("Форма правления", new["government"]),
                            ("Религия", new["religion"]),
                            ("Валюта", new["currency"]),
                        ):
                            index = next(
                                (
                                    i
                                    for i, field in enumerate(embed.fields)
                                    if field.name == field_name
                                ),
                                None,
                            )
                            if index is not None:
                                embed.set_field_at(
                                    index, name=field_name, value=field_value, inline=True
                                )
                        await card.edit(embed=embed)
                except discord.HTTPException:
                    log.exception("Unable to update the country welcome card")
        await interaction.response.send_message(
            "Карточка государства обновлена; изменения записаны в аудит.", ephemeral=True
        )

    @country_edit.autocomplete("country_id")
    async def edit_country_autocomplete(interaction: discord.Interaction, current: str):
        return await country_choices(interaction, current)
