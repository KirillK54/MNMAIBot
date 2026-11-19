import json
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import discord
from discord import app_commands
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

from vpi_bot.commands.autocomplete import country_choices
from vpi_bot.models import (
    AuditLog,
    Country,
    CountryResource,
    EconomicStats,
    GamePost,
    GuildConfig,
    MilitaryStats,
    VerdictOutbox,
)
from vpi_bot.registration import RegistrationModal, registration_allowed
from vpi_bot.security import admin_check
from vpi_bot.ui.cards import country_overview_embed
from vpi_bot.ui.setup import SetupWizardView, setup_dashboard_embed
from vpi_bot.ui.views import CountryPanel, CountrySelectView


def register(bot) -> None:
    @bot.tree.command(name="setup", description="Первоначальная настройка игрового сервера")
    @app_commands.describe(
        category="Категория приватных каналов государств; без неё откроется мастер настройки",
        registered_role="Роль, выдаваемая игроку после /register",
        admin_role="Дополнительная роль с доступом к каналам государств",
        moderator_role="Роль модератора с доступом к каналам государств",
        system_channel="Канал объявлений о регистрации государств",
        reports_channel="Канал ежедневных экономических отчётов",
        allow_multiple_countries="Разрешить игроку регистрировать несколько государств",
        timezone="Часовой пояс IANA игрового дня, например Europe/Saratov",
        daily_post_limit="Суточный лимит постов на игрока от 0 до 100",
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def setup(
        interaction: discord.Interaction,
        category: discord.CategoryChannel | None = None,
        registered_role: discord.Role | None = None,
        admin_role: discord.Role | None = None,
        moderator_role: discord.Role | None = None,
        system_channel: discord.TextChannel | None = None,
        reports_channel: discord.TextChannel | None = None,
        allow_multiple_countries: bool | None = None,
        timezone: str | None = None,
        daily_post_limit: app_commands.Range[int, 0, 100] | None = None,
    ):
        if not interaction.guild:
            await interaction.response.send_message(
                "Команда доступна только на сервере.", ephemeral=True
            )
            return
        if category is None:
            if (
                bot.settings.authorized_guild_id
                and bot.settings.authorized_guild_id != interaction.guild_id
            ):
                await interaction.response.send_message(
                    "Этот сервер не разрешён в конфигурации.", ephemeral=True
                )
                return
            async with bot.db() as session:
                existing = await session.scalar(
                    select(GuildConfig)
                    .where(GuildConfig.guild_id == interaction.guild_id)
                    .with_for_update()
                )
                other = await session.scalar(
                    select(GuildConfig.guild_id)
                    .where(GuildConfig.guild_id != interaction.guild_id)
                    .limit(1)
                )
                if other:
                    await interaction.response.send_message(
                        "Экземпляр уже привязан к другому Discord-серверу.", ephemeral=True
                    )
                    return
                if existing is None:
                    existing = GuildConfig(
                        guild_id=interaction.guild_id,
                        timezone=bot.settings.game_timezone,
                        daily_post_limit=bot.settings.global_daily_post_limit,
                        verdict_mode=bot.settings.verdict_mode,
                    )
                    session.add(existing)
                    session.add(
                        AuditLog(
                            guild_id=interaction.guild_id,
                            actor_id=interaction.user.id,
                            action="guild_setup_started",
                            target_type="guild",
                            target_id=str(interaction.guild_id),
                            new_value="wizard",
                        )
                    )
                from vpi_bot.seed import seed_guild_defaults

                await seed_guild_defaults(session, interaction.guild_id)
                await session.commit()
            embed = await setup_dashboard_embed(bot, interaction.guild)
            await interaction.response.send_message(
                embed=embed,
                view=SetupWizardView(bot, interaction.guild_id, interaction.user.id),
                ephemeral=True,
            )
            return
        requested_timezone, requested_daily_limit = timezone, daily_post_limit
        async with bot.db() as session:
            if (
                bot.settings.authorized_guild_id
                and bot.settings.authorized_guild_id != interaction.guild_id
            ):
                await interaction.response.send_message(
                    "Этот сервер не разрешён в конфигурации.", ephemeral=True
                )
                return
            existing = await session.scalar(
                select(GuildConfig)
                .where(GuildConfig.guild_id == interaction.guild_id)
                .with_for_update()
            )
            other = await session.scalar(
                select(GuildConfig.guild_id)
                .where(GuildConfig.guild_id != interaction.guild_id)
                .limit(1)
            )
            if other:
                await interaction.response.send_message(
                    "Экземпляр уже привязан к другому Discord-серверу.", ephemeral=True
                )
                return
            timezone = requested_timezone or (
                existing.timezone if existing else bot.settings.game_timezone
            )
            daily_post_limit = (
                requested_daily_limit
                if requested_daily_limit is not None
                else (
                    existing.daily_post_limit if existing else bot.settings.global_daily_post_limit
                )
            )
            try:
                ZoneInfo(timezone)
            except ZoneInfoNotFoundError:
                await interaction.response.send_message(
                    "Неизвестный часовой пояс IANA.", ephemeral=True
                )
                return
            old = (
                {
                    "category_id": existing.category_id,
                    "registered_role_id": existing.registered_role_id,
                    "admin_role_id": existing.admin_role_id,
                    "moderator_role_id": existing.moderator_role_id,
                    "system_channel_id": existing.system_channel_id,
                    "reports_channel_id": existing.reports_channel_id,
                    "allow_multiple_countries": existing.allow_multiple_countries,
                    "timezone": existing.timezone,
                    "daily_post_limit": existing.daily_post_limit,
                }
                if existing
                else None
            )
            config = existing or GuildConfig(
                guild_id=interaction.guild_id, verdict_mode=bot.settings.verdict_mode
            )
            config.category_id = category.id
            if registered_role is not None:
                config.registered_role_id = registered_role.id
            if admin_role is not None:
                config.admin_role_id = admin_role.id
            if moderator_role is not None:
                config.moderator_role_id = moderator_role.id
            if system_channel is not None:
                config.system_channel_id = system_channel.id
            if reports_channel is not None:
                config.reports_channel_id = reports_channel.id
            if allow_multiple_countries is not None:
                config.allow_multiple_countries = allow_multiple_countries
            config.timezone, config.daily_post_limit = timezone, daily_post_limit
            new = {
                "category_id": config.category_id,
                "registered_role_id": config.registered_role_id,
                "admin_role_id": config.admin_role_id,
                "moderator_role_id": config.moderator_role_id,
                "system_channel_id": config.system_channel_id,
                "reports_channel_id": config.reports_channel_id,
                "allow_multiple_countries": config.allow_multiple_countries,
                "timezone": config.timezone,
                "daily_post_limit": config.daily_post_limit,
            }
            session.add(config)
            session.add(
                AuditLog(
                    guild_id=interaction.guild_id,
                    actor_id=interaction.user.id,
                    action="guild_setup",
                    target_type="guild",
                    target_id=str(interaction.guild_id),
                    old_value=json.dumps(old, ensure_ascii=False) if old else None,
                    new_value=json.dumps(new, ensure_ascii=False),
                )
            )
            from vpi_bot.seed import seed_guild_defaults

            await seed_guild_defaults(session, interaction.guild_id)
            await session.commit()
        await interaction.response.send_message(
            "Сервер настроен. Теперь можно использовать /register.", ephemeral=True
        )

    @bot.tree.command(name="register", description="Зарегистрировать игровое государство")
    async def register(interaction: discord.Interaction):
        config = await bot.guild_config(interaction.guild_id)
        if config is None:
            await interaction.response.send_message(
                "Сервер не авторизован или ещё не настроен.", ephemeral=True
            )
            return
        async with bot.db() as session:
            allowed = await registration_allowed(
                session, interaction.guild_id, interaction.user.id, config.allow_multiple_countries
            )
        if not allowed:
            await interaction.response.send_message(
                "У вас уже зарегистрировано государство.", ephemeral=True
            )
            return
        await interaction.response.send_modal(RegistrationModal(bot))

    @bot.tree.command(name="panel", description="Открыть панель управления государством")
    @app_commands.describe(country_id="ID вашего государства; без ID откроется выбор")
    async def panel(interaction: discord.Interaction, country_id: int | None = None):
        if not interaction.guild_id or not await bot.guild_config(interaction.guild_id):
            await interaction.response.send_message(
                "Сервер не авторизован или не настроен.", ephemeral=True
            )
            return
        async with bot.db() as session:
            countries = (
                await session.scalars(
                    select(Country)
                    .where(
                        Country.guild_id == interaction.guild_id,
                        Country.owner_id == interaction.user.id,
                        Country.is_active.is_(True),
                    )
                    .order_by(Country.id)
                )
            ).all()
            country = (
                next((row for row in countries if row.id == country_id), None)
                if country_id
                else None
            )
            if country is None and country_id is None and len(countries) == 1:
                country = countries[0]
            stats = await session.get(EconomicStats, country.id) if country else None
            army = await session.get(MilitaryStats, country.id) if country else None
            resource_count = (
                await session.scalar(
                    select(func.count(CountryResource.resource_id)).where(
                        CountryResource.country_id == country.id
                    )
                )
                if country
                else 0
            )
        if not countries:
            await interaction.response.send_message(
                "Сначала зарегистрируйте государство командой /register.", ephemeral=True
            )
            return
        if country is None and country_id is not None:
            await interaction.response.send_message(
                "Вы не владеете государством с таким ID.", ephemeral=True
            )
            return
        if country is None and len(countries) <= 25:
            await interaction.response.send_message(
                "Выберите государство:",
                view=CountrySelectView(bot, interaction.user.id, countries),
                ephemeral=True,
            )
            return
        if country is None:
            await interaction.response.send_message(
                "У вас больше 25 государств. Выберите страну через автодополнение поля `country_id`.",
                ephemeral=True,
            )
            return
        if stats is None:
            await interaction.response.send_message(
                "Экономическая статистика не найдена.", ephemeral=True
            )
            return
        embed = country_overview_embed(country, stats, army, resource_count=resource_count or 0)
        await interaction.response.send_message(
            embed=embed, view=CountryPanel(bot, country.id, interaction.user.id), ephemeral=True
        )

    @panel.autocomplete("country_id")
    async def panel_country_autocomplete(interaction: discord.Interaction, current: str):
        return await country_choices(interaction, current, owner_only=True)

    @bot.tree.command(
        name="verdict-mode", description="Установить автоматическое применение или модерацию"
    )
    @app_commands.describe(
        mode="Автоматически применить проверенные изменения или ждать решения модератора"
    )
    @admin_check(bot)
    @app_commands.choices(
        mode=[
            app_commands.Choice(
                name="Автоматически применять проверенные изменения", value="automatic"
            ),
            app_commands.Choice(name="Только после утверждения администратора", value="moderation"),
        ]
    )
    async def verdict_mode(interaction: discord.Interaction, mode: app_commands.Choice[str]):
        async with bot.db() as session:
            config = await session.get(GuildConfig, interaction.guild_id)
            if not config or not await bot.guild_config(interaction.guild_id):
                await interaction.response.send_message("Сервер не настроен.", ephemeral=True)
                return
            old = config.verdict_mode
            config.verdict_mode = mode.value
            session.add(
                AuditLog(
                    guild_id=interaction.guild_id,
                    actor_id=interaction.user.id,
                    action="verdict_mode",
                    target_type="guild",
                    target_id=str(interaction.guild_id),
                    old_value=old,
                    new_value=mode.value,
                )
            )
            await session.commit()
        await interaction.response.send_message(
            f"Режим вердиктов: **{mode.name}**.", ephemeral=True
        )

    @bot.tree.command(name="game-rules", description="Изменить общие правила мира для ИИ-судьи")
    @app_commands.describe(
        rules="Полный текст правил, которые ИИ учитывает при оценке игровых постов"
    )
    @admin_check(bot)
    async def game_rules(interaction: discord.Interaction, rules: str):
        async with bot.db() as session:
            config = await session.get(GuildConfig, interaction.guild_id)
            if not config or not await bot.guild_config(interaction.guild_id):
                await interaction.response.send_message("Сервер не настроен.", ephemeral=True)
                return
            old = config.game_rules
            config.game_rules = rules[:8000]
            session.add(
                AuditLog(
                    guild_id=interaction.guild_id,
                    actor_id=interaction.user.id,
                    action="game_rules",
                    target_type="guild",
                    target_id=str(interaction.guild_id),
                    old_value=old[:4000] if old else None,
                    new_value=config.game_rules[:4000],
                )
            )
            await session.commit()
        await interaction.response.send_message("Правила игрового мира сохранены.", ephemeral=True)

    @bot.tree.command(name="post-limit", description="Изменить суточный лимит постов на игрока")
    @app_commands.describe(limit="Максимум игровых постов одного игрока за игровой день (0–100)")
    @admin_check(bot)
    async def post_limit(interaction: discord.Interaction, limit: app_commands.Range[int, 0, 100]):
        async with bot.db() as session:
            config = await session.get(GuildConfig, interaction.guild_id)
            if not config or not await bot.guild_config(interaction.guild_id):
                await interaction.response.send_message("Сервер не настроен.", ephemeral=True)
                return
            old = config.daily_post_limit
            config.daily_post_limit = limit
            session.add(
                AuditLog(
                    guild_id=interaction.guild_id,
                    actor_id=interaction.user.id,
                    action="post_limit",
                    target_type="guild",
                    target_id=str(interaction.guild_id),
                    old_value=str(old),
                    new_value=str(limit),
                )
            )
            await session.commit()
        await interaction.response.send_message(
            f"Глобальный лимит теперь: {limit} пост(а/ов) в сутки.", ephemeral=True
        )

    @bot.tree.command(
        name="country-multiple",
        description="Разрешить или запретить игрокам иметь несколько государств",
    )
    @app_commands.describe(allow="Да — несколько государств одному игроку; нет — только одно")
    @admin_check(bot)
    async def country_multiple(interaction: discord.Interaction, allow: bool):
        async with bot.db() as session:
            config = await session.scalar(
                select(GuildConfig)
                .where(GuildConfig.guild_id == interaction.guild_id)
                .with_for_update()
            )
            if not config or not await bot.guild_config(interaction.guild_id):
                await interaction.response.send_message("Сервер не настроен.", ephemeral=True)
                return
            old = config.allow_multiple_countries
            config.allow_multiple_countries = allow
            session.add(
                AuditLog(
                    guild_id=interaction.guild_id,
                    actor_id=interaction.user.id,
                    action="country_multiple",
                    target_type="guild",
                    target_id=str(interaction.guild_id),
                    old_value=str(old),
                    new_value=str(allow),
                )
            )
            await session.commit()
        await interaction.response.send_message(
            "Множественная регистрация "
            + ("разрешена." if allow else "запрещена для новых регистраций."),
            ephemeral=True,
        )

    @bot.tree.command(name="admin", description="Сводка административных настроек")
    @admin_check(bot)
    async def admin(interaction: discord.Interaction):
        try:
            config = await bot.guild_config(interaction.guild_id)
        except SQLAlchemyError:
            if not interaction.permissions.administrator:
                raise
            embed = discord.Embed(title="⚙️ Панель администратора", color=0xE74C3C)
            embed.add_field(name="База данных", value="недоступна", inline=True)
            embed.add_field(
                name="Ключ ИИ",
                value="настроен" if bot.settings.ai_enabled else "не настроен",
                inline=True,
            )
            embed.description = "Настройки сервера и счётчики очереди пока недоступны."
            await interaction.response.send_message(
                embed=embed, ephemeral=True, allowed_mentions=discord.AllowedMentions.none()
            )
            return
        if config is None:
            await interaction.response.send_message(
                "Сервер не авторизован или не настроен.", ephemeral=True
            )
            return
        database_status, pending_replies, failed_posts = "доступна", 0, 0
        try:
            async with bot.db() as session:
                await session.execute(select(1))
                pending_replies = (
                    await session.scalar(
                        select(func.count(VerdictOutbox.id)).where(
                            VerdictOutbox.guild_id == interaction.guild_id,
                            VerdictOutbox.status != "sent",
                        )
                    )
                    or 0
                )
                failed_posts = (
                    await session.scalar(
                        select(func.count(GamePost.id)).where(
                            GamePost.guild_id == interaction.guild_id,
                            GamePost.state == "failed",
                        )
                    )
                    or 0
                )
        except SQLAlchemyError:
            database_status, pending_replies, failed_posts = "недоступна", "?", "?"
        embed = discord.Embed(title="⚙️ Панель администратора", color=0x5865F2)
        embed.add_field(name="Эпоха", value=config.epoch_name, inline=True)
        embed.add_field(
            name="Лимит постов в сутки", value=str(config.daily_post_limit), inline=True
        )
        embed.add_field(name="Режим вердиктов", value=config.verdict_mode, inline=True)
        embed.add_field(
            name="Множественная регистрация",
            value="Разрешена" if config.allow_multiple_countries else "Запрещена",
            inline=True,
        )
        embed.add_field(name="Часовой пояс", value=config.timezone, inline=True)
        embed.add_field(
            name="Состояние сервисов",
            value=(
                f"База данных: {database_status}\n"
                f"Ключ ИИ: {'настроен' if bot.settings.ai_enabled else 'не настроен'}\n"
                f"Недоставлено вердиктов: {pending_replies}\n"
                f"Постов для повтора: {failed_posts}"
            ),
            inline=False,
        )
        embed.add_field(
            name="Каналы",
            value=(
                f"Системный: {f'<#{config.system_channel_id}>' if config.system_channel_id else '—'}\n"
                f"Отчёты: {f'<#{config.reports_channel_id}>' if config.reports_channel_id else '—'}"
            ),
            inline=False,
        )
        embed.add_field(
            name="Управление",
            value="Начальная настройка: /setup\nГосударства: /countries\nПроверка вердиктов: /verdict-review",
            inline=False,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @bot.tree.command(
        name="countries", description="Список зарегистрированных государств (администратор)"
    )
    @admin_check(bot)
    async def countries(interaction: discord.Interaction):
        async with bot.db() as session:
            rows = (
                await session.scalars(
                    select(Country).where(
                        Country.guild_id == interaction.guild_id, Country.is_active.is_(True)
                    )
                )
            ).all()
        await interaction.response.send_message(
            "\n".join(f"`{c.id}` **{c.name}** — <@{c.owner_id}>" for c in rows)
            or "Государств пока нет.",
            ephemeral=True,
        )
