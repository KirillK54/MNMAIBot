import discord
from discord import app_commands
from sqlalchemy import or_, select

from vpi_bot.commands.autocomplete import country_choices, lore_choices
from vpi_bot.models import Country, CountryLore, HistoricalEvent, LoreSummary
from vpi_bot.security import admin_check


def register(bot) -> None:
    @bot.tree.command(name="lore-edit", description="Отредактировать запись лора по ID")
    @app_commands.describe(
        lore_id="ID записи лора из карточки государства",
        title="Новое короткое название записи",
        content="Новый текст знания или предыстории",
        category="Раздел лора, например история, культура или политика",
        importance="Важность для памяти судьи от 1 до 10",
    )
    @admin_check(bot)
    async def lore_edit(
        interaction: discord.Interaction,
        lore_id: int,
        title: str,
        content: str,
        category: str = "general",
        importance: app_commands.Range[int, 1, 10] = 5,
    ):
        from vpi_bot.models import AuditLog

        async with bot.db() as session:
            row = await session.scalar(
                select(CountryLore)
                .join(Country, CountryLore.country_id == Country.id)
                .where(CountryLore.id == lore_id, Country.guild_id == interaction.guild_id)
            )
            if not row:
                await interaction.response.send_message("Запись лора не найдена.", ephemeral=True)
                return
            old = f"{row.category}|{row.title}|{row.content}"
            row.category, row.title = category[:80], title[:200]
            row.content, row.importance = content[:8000], importance
            session.add(
                AuditLog(
                    guild_id=interaction.guild_id,
                    actor_id=interaction.user.id,
                    action="lore_edit",
                    target_type="lore",
                    target_id=str(row.id),
                    old_value=old[:4000],
                    new_value=f"{row.category}|{row.title}|{row.content}"[:4000],
                )
            )
            await session.commit()
        await interaction.response.send_message("Запись лора обновлена.", ephemeral=True)

    @lore_edit.autocomplete("lore_id")
    @admin_check(bot)
    async def lore_edit_autocomplete(interaction: discord.Interaction, current: str):
        return await lore_choices(interaction, current)

    @bot.tree.command(name="lore-delete", description="Удалить запись лора по ID")
    @app_commands.describe(
        lore_id="ID удаляемой записи лора",
        reason="Причина удаления; сохранится в журнале аудита",
    )
    @admin_check(bot)
    async def lore_delete(interaction: discord.Interaction, lore_id: int, reason: str):
        from vpi_bot.models import AuditLog

        async with bot.db() as session:
            row = await session.scalar(
                select(CountryLore)
                .join(Country, CountryLore.country_id == Country.id)
                .where(CountryLore.id == lore_id, Country.guild_id == interaction.guild_id)
                .with_for_update()
            )
            if not row:
                await interaction.response.send_message("Запись лора не найдена.", ephemeral=True)
                return
            session.add(
                AuditLog(
                    guild_id=interaction.guild_id,
                    actor_id=interaction.user.id,
                    action="lore_delete",
                    target_type="lore",
                    target_id=str(row.id),
                    old_value=f"{row.title}: {row.content}"[:4000],
                    new_value=None,
                    reason=reason[:1000],
                )
            )
            await session.delete(row)
            await session.commit()
        await interaction.response.send_message(
            "Запись лора удалена; изменение записано в аудит.", ephemeral=True
        )

    @lore_delete.autocomplete("lore_id")
    @admin_check(bot)
    async def lore_delete_autocomplete(interaction: discord.Interaction, current: str):
        return await lore_choices(interaction, current)

    @bot.tree.command(
        name="event-add", description="Добавить важное историческое событие государства"
    )
    @app_commands.describe(
        country_id="ID государства из /countries",
        title="Короткое название события",
        content="Что произошло и как это повлияло на государство",
        importance="Важность события для памяти судьи от 1 до 10",
        tags="Необязательные теги через запятую",
    )
    @admin_check(bot)
    async def event_add(
        interaction: discord.Interaction,
        country_id: int,
        title: str,
        content: str,
        importance: app_commands.Range[int, 1, 10] = 5,
        tags: str = "",
    ):
        from vpi_bot.lore import refresh_event_summary
        from vpi_bot.models import AuditLog

        async with bot.db() as session:
            country = await session.scalar(
                select(Country)
                .where(Country.id == country_id, Country.guild_id == interaction.guild_id)
                .with_for_update()
            )
            if not country:
                await interaction.response.send_message("Государство не найдено.", ephemeral=True)
                return
            event = HistoricalEvent(
                country_id=country.id,
                title=title[:200],
                content=content[:8000],
                importance=importance,
                tags=[tag.strip()[:60] for tag in tags.split(",") if tag.strip()][:20],
            )
            session.add(event)
            await session.flush()
            await refresh_event_summary(session, country.id)
            session.add(
                AuditLog(
                    guild_id=interaction.guild_id,
                    actor_id=interaction.user.id,
                    action="event_add",
                    target_type="country",
                    target_id=str(country.id),
                    new_value=f"{event.title}: {event.content}"[:4000],
                    reason="Историческое событие добавлено администратором",
                )
            )
            await session.commit()
        await interaction.response.send_message(
            "Историческое событие сохранено и включено в память судьи.", ephemeral=True
        )

    @event_add.autocomplete("country_id")
    @admin_check(bot)
    async def event_country_autocomplete(interaction: discord.Interaction, current: str):
        return await country_choices(interaction, current)

    @bot.tree.command(
        name="lore-summary-set", description="Заменить постоянную сводку лора государства"
    )
    @app_commands.describe(
        country_id="ID государства из /countries",
        content="Полный текст постоянной сводки, учитываемой судьёй",
    )
    @admin_check(bot)
    async def lore_summary_set(interaction: discord.Interaction, country_id: int, content: str):
        from vpi_bot.models import AuditLog

        async with bot.db() as session:
            country = await session.scalar(
                select(Country)
                .where(Country.id == country_id, Country.guild_id == interaction.guild_id)
                .with_for_update()
            )
            if not country:
                await interaction.response.send_message("Государство не найдено.", ephemeral=True)
                return
            summary = await session.scalar(
                select(LoreSummary).where(LoreSummary.country_id == country.id).with_for_update()
            )
            old = summary.content if summary else None
            through_id = await session.scalar(
                select(HistoricalEvent.id)
                .where(HistoricalEvent.country_id == country.id)
                .order_by(HistoricalEvent.id.desc())
                .limit(1)
            )
            if summary is None:
                summary = LoreSummary(country_id=country.id)
                session.add(summary)
            summary.content, summary.through_event_id = content[:12000], through_id
            session.add(
                AuditLog(
                    guild_id=interaction.guild_id,
                    actor_id=interaction.user.id,
                    action="lore_summary_set",
                    target_type="country",
                    target_id=str(country.id),
                    old_value=old[:4000] if old else None,
                    new_value=content[:4000],
                )
            )
            await session.commit()
        await interaction.response.send_message("Постоянная сводка сохранена.", ephemeral=True)

    @lore_summary_set.autocomplete("country_id")
    @admin_check(bot)
    async def summary_country_autocomplete(interaction: discord.Interaction, current: str):
        return await country_choices(interaction, current)

    @bot.tree.command(name="history-search", description="Найти события и записи лора на сервере")
    @app_commands.describe(query="Слово или фраза длиной минимум 2 символа")
    @admin_check(bot)
    async def history_search(interaction: discord.Interaction, query: str):
        needle = f"%{query.strip()[:100]}%"
        if len(query.strip()) < 2:
            await interaction.response.send_message(
                "Введите не менее двух символов для поиска.", ephemeral=True
            )
            return
        async with bot.db() as session:
            events = (
                await session.execute(
                    select(
                        Country.name,
                        HistoricalEvent.title,
                        HistoricalEvent.content,
                        HistoricalEvent.event_date,
                    )
                    .join(Country, HistoricalEvent.country_id == Country.id)
                    .where(
                        Country.guild_id == interaction.guild_id,
                        or_(
                            HistoricalEvent.title.ilike(needle),
                            HistoricalEvent.content.ilike(needle),
                        ),
                    )
                    .order_by(HistoricalEvent.importance.desc(), HistoricalEvent.event_date.desc())
                    .limit(10)
                )
            ).all()
            lore = (
                await session.execute(
                    select(Country.name, CountryLore.title, CountryLore.content)
                    .join(Country, CountryLore.country_id == Country.id)
                    .where(
                        Country.guild_id == interaction.guild_id,
                        or_(CountryLore.title.ilike(needle), CountryLore.content.ilike(needle)),
                    )
                    .order_by(CountryLore.importance.desc())
                    .limit(10)
                )
            ).all()
        lines = [f"📜 **{name}** · {title} — {content[:180]}" for name, title, content, _ in events]
        lines += [f"📖 **{name}** · {title} — {content[:180]}" for name, title, content in lore]
        await interaction.response.send_message(
            "\n".join(lines[:15]) or "Совпадений не найдено.", ephemeral=True
        )
