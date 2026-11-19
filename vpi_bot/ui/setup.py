from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import discord
from sqlalchemy import select

from vpi_bot.models import AuditLog, GuildConfig


async def setup_dashboard_embed(bot, guild: discord.Guild) -> discord.Embed:
    async with bot.db() as session:
        config = await session.get(GuildConfig, guild.id)
    if config is None:
        return discord.Embed(
            title="⚙️ Настройка игрового сервера",
            description="Настройки ещё не созданы. Выберите значения в меню ниже.",
            color=0xE67E22,
        )

    def channel_label(channel_id: int | None) -> str:
        if channel_id is None:
            return "⚠️ Не выбран"
        channel = guild.get_channel(channel_id)
        return channel.mention if channel else f"⚠️ Канал недоступен (`{channel_id}`)"

    def role_label(role_id: int | None) -> str:
        if role_id is None:
            return "Не выбрана"
        role = guild.get_role(role_id)
        return role.mention if role else f"Роль недоступна (`{role_id}`)"

    embed = discord.Embed(
        title="⚙️ Панель настройки игрового сервера",
        description=(
            "Изменения сохраняются сразу после выбора. Для начала обязательно выберите категорию; "
            "остальные настройки можно заполнить позже. Нажмите **Обновить**, чтобы проверить статус."
        ),
        color=0x5865F2,
    )
    embed.add_field(
        name="📁 Категория государств", value=channel_label(config.category_id), inline=False
    )
    embed.add_field(
        name="📣 Системный / отчётный канал",
        value=f"{channel_label(config.system_channel_id)} / {channel_label(config.reports_channel_id)}",
        inline=False,
    )
    embed.add_field(
        name="🎭 Роли игроков / администраторов / модераторов",
        value=(
            f"{role_label(config.registered_role_id)} / {role_label(config.admin_role_id)} / "
            f"{role_label(config.moderator_role_id)}"
        ),
        inline=False,
    )
    embed.add_field(
        name="🕰️ Игровой день",
        value=f"{config.timezone} · лимит {config.daily_post_limit} пост(а) на игрока",
        inline=True,
    )
    embed.add_field(
        name="🌐 Государства",
        value="Можно несколько" if config.allow_multiple_countries else "Одно на игрока",
        inline=True,
    )
    embed.add_field(
        name="⚖️ Вердикты",
        value="Автоматически" if config.verdict_mode == "automatic" else "Через модерацию",
        inline=True,
    )
    embed.add_field(
        name="🕰️ Эпоха мира",
        value=f"{config.epoch_name} · сменить через /epoch-set",
        inline=False,
    )
    embed.set_footer(text="Выбор категории обязателен перед /register")
    return embed


async def _save_field(bot, interaction: discord.Interaction, field: str, value) -> None:
    async with bot.db() as session:
        config = await session.scalar(
            select(GuildConfig)
            .where(GuildConfig.guild_id == interaction.guild_id)
            .with_for_update()
        )
        if config is None:
            await interaction.response.send_message(
                "Сначала откройте /setup, чтобы создать конфигурацию сервера.", ephemeral=True
            )
            return
        old_value = getattr(config, field)
        setattr(config, field, value)
        session.add(
            AuditLog(
                guild_id=interaction.guild_id,
                actor_id=interaction.user.id,
                action=f"setup_{field}",
                target_type="guild",
                target_id=str(interaction.guild_id),
                old_value=str(old_value) if old_value is not None else None,
                new_value=str(value) if value is not None else None,
            )
        )
        await session.commit()
    await interaction.response.edit_message(
        content="✅ Сохранено. Вернитесь к карточке настройки и нажмите «Обновить».",
        embed=None,
        view=None,
    )


class _SetupSelectView(discord.ui.View):
    def __init__(self, owner_id: int):
        super().__init__(timeout=300)
        self.owner_id = owner_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "Эта настройка открыта другим администратором.", ephemeral=True
            )
            return False
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message(
                "Настраивать сервер может только администратор.", ephemeral=True
            )
            return False
        return True


class _ChannelSettingSelect(discord.ui.ChannelSelect):
    def __init__(self, bot, owner_id: int, field: str, label: str, channel_types, row: int):
        super().__init__(
            channel_types=channel_types,
            placeholder=label,
            min_values=0 if field != "category_id" else 1,
            max_values=1,
            row=row,
        )
        self.bot, self.field = bot, field

    async def callback(self, interaction: discord.Interaction) -> None:
        channel_id = self.values[0].id if self.values else None
        await _save_field(self.bot, interaction, self.field, channel_id)


class _RoleSettingSelect(discord.ui.RoleSelect):
    def __init__(self, bot, field: str, label: str, row: int):
        super().__init__(placeholder=label, min_values=0, max_values=1, row=row)
        self.bot, self.field = bot, field

    async def callback(self, interaction: discord.Interaction) -> None:
        role_id = self.values[0].id if self.values else None
        await _save_field(self.bot, interaction, self.field, role_id)


class SetupChannelsView(_SetupSelectView):
    def __init__(self, bot, owner_id: int):
        super().__init__(owner_id)
        self.add_item(
            _ChannelSettingSelect(
                bot,
                owner_id,
                "category_id",
                "① Выберите категорию каналов государств",
                [discord.ChannelType.category],
                0,
            )
        )
        self.add_item(
            _ChannelSettingSelect(
                bot,
                owner_id,
                "system_channel_id",
                "② Системный канал (можно очистить)",
                [discord.ChannelType.text],
                1,
            )
        )
        self.add_item(
            _ChannelSettingSelect(
                bot,
                owner_id,
                "reports_channel_id",
                "③ Канал отчётов (можно очистить)",
                [discord.ChannelType.text],
                2,
            )
        )


class SetupRolesView(_SetupSelectView):
    def __init__(self, bot, owner_id: int):
        super().__init__(owner_id)
        for row, (field, label) in enumerate(
            (
                ("registered_role_id", "Роль зарегистрированного игрока (необязательно)"),
                ("admin_role_id", "Дополнительная роль администратора (необязательно)"),
                ("moderator_role_id", "Роль модератора (необязательно)"),
            )
        ):
            self.add_item(_RoleSettingSelect(bot, field, label, row))


class SetupPreferencesModal(discord.ui.Modal, title="Основные параметры игры"):
    timezone_input = discord.ui.TextInput(
        label="Часовой пояс IANA",
        placeholder="Europe/Saratov",
        max_length=64,
    )
    limit_input = discord.ui.TextInput(
        label="Постов на игрока в сутки (0–100)",
        placeholder="3",
        max_length=3,
    )
    multiple_input = discord.ui.TextInput(
        label="Несколько государств одному игроку? (да/нет)",
        placeholder="нет",
        max_length=3,
    )

    def __init__(self, bot, owner_id: int, timezone: str, daily_limit: int, allow_multiple: bool):
        super().__init__(timeout=300)
        self.bot, self.owner_id = bot, owner_id
        self.timezone_input.default = timezone
        self.limit_input.default = str(daily_limit)
        self.multiple_input.default = "да" if allow_multiple else "нет"

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if (
            interaction.user.id != self.owner_id
            or not interaction.user.guild_permissions.administrator
        ):
            await interaction.response.send_message(
                "Только открывший мастер администратор может сохранить настройки.", ephemeral=True
            )
            return
        timezone = self.timezone_input.value.strip()
        try:
            ZoneInfo(timezone)
            daily_limit = int(self.limit_input.value)
        except (ZoneInfoNotFoundError, ValueError):
            await interaction.response.send_message(
                "Укажите существующий часовой пояс IANA и целый лимит от 0 до 100.", ephemeral=True
            )
            return
        if not 0 <= daily_limit <= 100:
            await interaction.response.send_message(
                "Лимит должен быть от 0 до 100.", ephemeral=True
            )
            return
        multiple_text = self.multiple_input.value.strip().casefold()
        if multiple_text not in {"да", "нет", "yes", "no"}:
            await interaction.response.send_message(
                "Для нескольких государств введите «да» или «нет».", ephemeral=True
            )
            return
        async with self.bot.db() as session:
            config = await session.scalar(
                select(GuildConfig)
                .where(GuildConfig.guild_id == interaction.guild_id)
                .with_for_update()
            )
            if config is None:
                await interaction.response.send_message("Сначала откройте /setup.", ephemeral=True)
                return
            old = {
                "timezone": config.timezone,
                "daily_post_limit": config.daily_post_limit,
                "allow_multiple_countries": config.allow_multiple_countries,
            }
            config.timezone = timezone
            config.daily_post_limit = daily_limit
            config.allow_multiple_countries = multiple_text in {"да", "yes"}
            session.add(
                AuditLog(
                    guild_id=interaction.guild_id,
                    actor_id=interaction.user.id,
                    action="setup_preferences",
                    target_type="guild",
                    target_id=str(interaction.guild_id),
                    old_value=str(old),
                    new_value=str(
                        {
                            "timezone": timezone,
                            "daily_post_limit": daily_limit,
                            "allow_multiple_countries": config.allow_multiple_countries,
                        }
                    ),
                )
            )
            await session.commit()
        await interaction.response.send_message("✅ Основные параметры сохранены.", ephemeral=True)


class SetupWizardView(discord.ui.View):
    def __init__(self, bot, guild_id: int, owner_id: int):
        super().__init__(timeout=900)
        self.bot, self.guild_id, self.owner_id = bot, guild_id, owner_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "Этот мастер открыт другим администратором.", ephemeral=True
            )
            return False
        if (
            interaction.guild_id != self.guild_id
            or not interaction.user.guild_permissions.administrator
        ):
            await interaction.response.send_message(
                "Настраивать сервер может только его администратор.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="Каналы", emoji="📣", style=discord.ButtonStyle.primary, row=0)
    async def channels(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            "Выберите категорию, системный канал и канал отчётов. Каждый выбор сохраняется сразу.",
            view=SetupChannelsView(self.bot, self.owner_id),
            ephemeral=True,
        )

    @discord.ui.button(label="Роли", emoji="🎭", style=discord.ButtonStyle.secondary, row=0)
    async def roles(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            "Задайте роли; необязательную роль можно очистить пустым выбором.",
            view=SetupRolesView(self.bot, self.owner_id),
            ephemeral=True,
        )

    @discord.ui.button(label="Параметры", emoji="⚙️", style=discord.ButtonStyle.secondary, row=0)
    async def preferences(self, interaction: discord.Interaction, button: discord.ui.Button):
        async with self.bot.db() as session:
            config = await session.get(GuildConfig, self.guild_id)
        if config is None:
            await interaction.response.send_message(
                "Конфигурация сервера не найдена.", ephemeral=True
            )
            return
        await interaction.response.send_modal(
            SetupPreferencesModal(
                self.bot,
                self.owner_id,
                config.timezone,
                config.daily_post_limit,
                config.allow_multiple_countries,
            )
        )

    @discord.ui.button(label="Обновить", emoji="🔄", style=discord.ButtonStyle.secondary, row=1)
    async def refresh(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = await setup_dashboard_embed(self.bot, interaction.guild)
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="Готово", emoji="✅", style=discord.ButtonStyle.success, row=1)
    async def finish(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = await setup_dashboard_embed(self.bot, interaction.guild)
        async with self.bot.db() as session:
            config = await session.get(GuildConfig, self.guild_id)
        if config is None or config.category_id is None:
            await interaction.response.send_message(
                "Для завершения обязательно выберите категорию каналов государств.", ephemeral=True
            )
            return
        embed.description = "✅ Сервер настроен. Игроки могут использовать /register, а посты отправлять командой !verdict."
        await interaction.response.edit_message(embed=embed, view=None)
        self.stop()
