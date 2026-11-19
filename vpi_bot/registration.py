import discord
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from vpi_bot.models import Country, EconomicStats, GuildConfig
from vpi_bot.ui.cards import country_overview_embed


async def registration_allowed(session, guild_id: int, user_id: int, allow_multiple: bool) -> bool:
    if allow_multiple:
        return True
    existing = await session.scalar(
        select(Country.id).where(
            Country.guild_id == guild_id,
            Country.owner_id == user_id,
            Country.is_active.is_(True),
        )
    )
    return existing is None


class RegistrationModal(discord.ui.Modal, title="Регистрация государства"):
    name = discord.ui.TextInput(label="Название государства", max_length=100)
    government = discord.ui.TextInput(
        label="Форма правления (Республика / Монархия)", max_length=40
    )
    religion = discord.ui.TextInput(label="Государственная религия", max_length=120)
    currency = discord.ui.TextInput(label="Национальная валюта", max_length=80)
    description = discord.ui.TextInput(
        label="Краткое описание (необязательно)",
        required=False,
        style=discord.TextStyle.paragraph,
        max_length=800,
    )

    def __init__(self, bot):
        super().__init__(timeout=300)
        self.bot = bot

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or not await self.bot.guild_config(interaction.guild_id):
            await interaction.response.send_message(
                "Сервер не настроен или не авторизован.", ephemeral=True
            )
            return
        if not all(
            value.strip()
            for value in (
                self.name.value,
                self.government.value,
                self.religion.value,
                self.currency.value,
            )
        ):
            await interaction.response.send_message(
                "Название, форма правления, религия и валюта не могут быть пустыми.", ephemeral=True
            )
            return
        government = self.government.value.strip()
        norm = government.casefold()
        if norm not in {"республика", "монархия"}:
            await interaction.response.send_message(
                "Форма правления должна быть «Республика» или «Монархия».", ephemeral=True
            )
            return
        async with self.bot.db() as session:
            config = await session.scalar(
                select(GuildConfig)
                .where(GuildConfig.guild_id == interaction.guild_id)
                .with_for_update()
            )
            if not config or not config.category_id:
                await interaction.response.send_message(
                    "Администратор должен настроить категорию каналов командой /setup.",
                    ephemeral=True,
                )
                return
            if not await registration_allowed(
                session, interaction.guild_id, interaction.user.id, config.allow_multiple_countries
            ):
                await interaction.response.send_message(
                    "У вас уже зарегистрировано государство.", ephemeral=True
                )
                return
            country = Country(
                guild_id=interaction.guild_id,
                owner_id=interaction.user.id,
                name=self.name.value.strip(),
                government=government,
                religion=self.religion.value.strip(),
                currency=self.currency.value.strip(),
                description=self.description.value.strip(),
            )
            country.economy = EconomicStats()
            session.add(country)
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                await interaction.response.send_message(
                    "Государство уже зарегистрировано.", ephemeral=True
                )
                return
            country_id = country.id
            category_id, role_id = config.category_id, config.registered_role_id
            admin_role_id, moderator_role_id = config.admin_role_id, config.moderator_role_id
            system_channel_id = config.system_channel_id
        await interaction.response.defer(ephemeral=True, thinking=True)
        category = interaction.guild.get_channel(category_id)
        if not isinstance(category, discord.CategoryChannel):
            await self._delete_country(country_id)
            await interaction.followup.send(
                "Настроенная категория больше не существует. Обратитесь к администратору.",
                ephemeral=True,
            )
            return
        admin_role = interaction.guild.get_role(admin_role_id) if admin_role_id else None
        moderator_role = (
            interaction.guild.get_role(moderator_role_id) if moderator_role_id else None
        )
        overwrites = {
            interaction.guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user: discord.PermissionOverwrite(
                view_channel=True, send_messages=True, read_message_history=True
            ),
            interaction.guild.me: discord.PermissionOverwrite(
                view_channel=True, send_messages=True, manage_channels=True
            ),
        }
        if admin_role:
            overwrites[admin_role] = discord.PermissionOverwrite(
                view_channel=True, send_messages=True, read_message_history=True
            )
        if moderator_role:
            overwrites[moderator_role] = discord.PermissionOverwrite(
                view_channel=True, send_messages=True, read_message_history=True
            )
        channel = None
        try:
            channel = await interaction.guild.create_text_channel(
                name=f"{country_id}-{self._slug(self.name.value)}"[:100],
                category=category,
                overwrites=overwrites,
                reason="Создание игрового канала государства",
            )
            async with self.bot.db() as session:
                row = await session.get(Country, country_id)
                row.channel_id = channel.id
                await session.commit()
            if role_id:
                role = interaction.guild.get_role(role_id)
                if role:
                    await interaction.user.add_roles(
                        role, reason="Регистрация игрового государства"
                    )
            async with self.bot.db() as session:
                country_card = await session.get(Country, country_id)
                economy_card = await session.get(EconomicStats, country_id)
            embed = country_overview_embed(country_card, economy_card)
            embed.add_field(name="🕰️ Эпоха", value=config.epoch_name, inline=True)
            embed.add_field(
                name="👤 Глава государства", value=interaction.user.mention, inline=True
            )
            welcome_message = await channel.send(embed=embed)
            async with self.bot.db() as session:
                row = await session.get(Country, country_id)
                row.info_message_id = welcome_message.id
                await session.commit()
            await interaction.followup.send(
                f"Государство зарегистрировано: {channel.mention}", ephemeral=True
            )
            if system_channel_id and system_channel_id != channel.id:
                system_channel = self.bot.get_channel(system_channel_id)
                if system_channel is None:
                    try:
                        system_channel = await self.bot.fetch_channel(system_channel_id)
                    except discord.HTTPException:
                        system_channel = None
                if system_channel:
                    try:
                        await system_channel.send(
                            f"🏛️ Зарегистрировано государство **{self.name.value.strip()}** "
                            f"от игрока {interaction.user.mention}: {channel.mention}.",
                            allowed_mentions=discord.AllowedMentions.none(),
                        )
                    except discord.HTTPException:
                        pass
        except discord.HTTPException:
            if channel is not None:
                try:
                    await channel.delete(reason="Откат неудачной регистрации государства")
                except discord.HTTPException:
                    pass
            await self._delete_country(country_id)
            await interaction.followup.send(
                "Не удалось создать канал. Проверьте права бота и настройки категории.",
                ephemeral=True,
            )

    async def _delete_country(self, country_id: int) -> None:
        async with self.bot.db() as session:
            country = await session.get(Country, country_id)
            if country:
                await session.delete(country)
                await session.commit()

    @staticmethod
    def _slug(value: str) -> str:
        import re

        text = re.sub(r"[^a-zA-Zа-яА-ЯёЁ0-9-]+", "-", value.lower()).strip("-")
        return text[:70] or "gosudarstvo"
