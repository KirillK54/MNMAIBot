import json
from decimal import Decimal

import discord
from discord import app_commands
from sqlalchemy import select

from vpi_bot.commands.autocomplete import country_choices
from vpi_bot.models import Country
from vpi_bot.security import admin_check


async def _resource_choices(
    interaction: discord.Interaction, current: str, *, enabled: bool | None = True
):
    from vpi_bot.models import ResourceDefinition

    if interaction.guild_id is None:
        return []
    async with interaction.client.db() as session:
        rows = (
            await session.execute(
                select(ResourceDefinition.name, ResourceDefinition.unit)
                .where(
                    ResourceDefinition.guild_id == interaction.guild_id,
                    ResourceDefinition.name.ilike(f"%{current}%"),
                    *([ResourceDefinition.enabled.is_(enabled)] if enabled is not None else []),
                )
                .order_by(ResourceDefinition.name)
                .limit(25)
            )
        ).all()
    return [app_commands.Choice(name=f"{name} · {unit}"[:100], value=name) for name, unit in rows]


def register(bot) -> None:
    @bot.tree.command(name="resource-catalog", description="Показать каталог доступных ресурсов")
    @admin_check(bot)
    async def resource_catalog(interaction: discord.Interaction):
        from vpi_bot.models import ResourceDefinition

        async with bot.db() as session:
            from vpi_bot.seed import seed_guild_defaults

            await seed_guild_defaults(session, interaction.guild_id)
            await session.commit()
            resources = (
                await session.scalars(
                    select(ResourceDefinition)
                    .where(ResourceDefinition.guild_id == interaction.guild_id)
                    .order_by(ResourceDefinition.name)
                )
            ).all()
        if not resources:
            await interaction.response.send_message(
                "Каталог пуст. Откройте /setup без параметров, чтобы загрузить стандартный набор.",
                ephemeral=True,
            )
            return
        lines = [
            f"{'✅' if resource.enabled else '⏸️'} **{resource.name}** · {resource.unit}"
            + (f" — {resource.description}" if resource.description else "")
            for resource in resources
        ]
        embed = discord.Embed(
            title="📦 Каталог ресурсов государства",
            description="\n".join(lines)[:4000],
            color=0x2ECC71,
        )
        embed.set_footer(
            text="Остатки задаются командой /resource-adjust — название подсказывает список"
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @bot.tree.command(name="resource-add", description="Добавить игровой ресурс")
    @app_commands.describe(
        name="Название ресурса, например «Стекло»",
        unit="Единица измерения, например т, кг, м³ или шт.",
        description="Коротко поясните, что учитывает ресурс",
    )
    @admin_check(bot)
    async def resource_add(
        interaction: discord.Interaction, name: str, unit: str = "ед.", description: str = ""
    ):
        if not await bot.guild_config(interaction.guild_id):
            await interaction.response.send_message("Сервер не настроен.", ephemeral=True)
            return
        from vpi_bot.models import AuditLog, ResourceDefinition

        async with bot.db() as session:
            exists = await session.scalar(
                select(ResourceDefinition.id).where(
                    ResourceDefinition.guild_id == interaction.guild_id,
                    ResourceDefinition.name == name.strip(),
                )
            )
            if exists:
                await interaction.response.send_message(
                    "Такой ресурс уже существует.", ephemeral=True
                )
                return
            resource = ResourceDefinition(
                guild_id=interaction.guild_id,
                name=name.strip()[:100],
                unit=unit[:30],
                description=description[:1000],
            )
            session.add(resource)
            await session.flush()
            session.add(
                AuditLog(
                    guild_id=interaction.guild_id,
                    actor_id=interaction.user.id,
                    action="resource_add",
                    target_type="resource",
                    target_id=str(resource.id),
                    new_value=f"{resource.name}|{resource.unit}|{resource.description}"[:4000],
                )
            )
            await session.commit()
        await interaction.response.send_message(f"Ресурс **{name}** добавлен.", ephemeral=True)

    @bot.tree.command(
        name="resource-adjust", description="Изменить запас ресурса государства (администратор)"
    )
    @app_commands.describe(
        country_id="ID государства из /countries",
        resource_name="Активный ресурс; выберите вариант из автодополнения",
        delta="Изменение запаса: плюс добавить, минус списать",
        reason="Причина изменения для журнала аудита",
    )
    @admin_check(bot)
    async def resource_adjust(
        interaction: discord.Interaction,
        country_id: int,
        resource_name: str,
        delta: app_commands.Range[float, -1000000000.0, 1000000000.0],
        reason: str,
    ):
        from vpi_bot.models import AuditLog, CountryResource, ResourceDefinition, ResourceOperation

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
            resource = await session.scalar(
                select(ResourceDefinition).where(
                    ResourceDefinition.guild_id == interaction.guild_id,
                    ResourceDefinition.name == resource_name,
                    ResourceDefinition.enabled.is_(True),
                )
            )
            if not country or not resource:
                await interaction.response.send_message(
                    "Государство или ресурс не найден.", ephemeral=True
                )
                return
            inventory = await session.scalar(
                select(CountryResource)
                .where(
                    CountryResource.country_id == country.id,
                    CountryResource.resource_id == resource.id,
                )
                .with_for_update()
            )
            if inventory is None:
                inventory = CountryResource(
                    country_id=country.id, resource_id=resource.id, quantity=Decimal(0)
                )
                session.add(inventory)
                await session.flush()
            before, change = Decimal(inventory.quantity), Decimal(str(delta))
            after = before + change
            if after < 0:
                await interaction.response.send_message(
                    "На складе недостаточно ресурсов.", ephemeral=True
                )
                return
            if after > Decimal("99999999999999999.999"):
                await interaction.response.send_message(
                    "Склад достиг максимальной вместимости.", ephemeral=True
                )
                return
            inventory.quantity = after
            session.add_all(
                [
                    ResourceOperation(
                        country_id=country.id,
                        resource_id=resource.id,
                        source="admin_adjust",
                        source_id=str(interaction.id),
                        delta=change,
                        before_value=before,
                        after_value=after,
                    ),
                    AuditLog(
                        guild_id=interaction.guild_id,
                        actor_id=interaction.user.id,
                        action="resource_adjust",
                        target_type="country",
                        target_id=str(country.id),
                        old_value=str(before),
                        new_value=str(after),
                        reason=reason[:1000],
                    ),
                ]
            )
            await session.commit()
        await interaction.response.send_message(
            f"{resource.name}: {before} → {after} {resource.unit}. Операция записана.",
            ephemeral=True,
        )

    @resource_adjust.autocomplete("resource_name")
    async def resource_adjust_autocomplete(interaction: discord.Interaction, current: str):
        return await _resource_choices(interaction, current)

    @resource_adjust.autocomplete("country_id")
    async def resource_country_autocomplete(interaction: discord.Interaction, current: str):
        return await country_choices(interaction, current)

    @bot.tree.command(
        name="resource-disable", description="Отключить ресурс, сохранив историю и склады"
    )
    @app_commands.describe(
        resource_name="Активный ресурс из списка автодополнения",
        reason="Причина отключения для аудита",
    )
    @admin_check(bot)
    async def resource_disable(interaction: discord.Interaction, resource_name: str, reason: str):
        from vpi_bot.models import AuditLog, ResourceDefinition, ResourceProduction

        async with bot.db() as session:
            resource = await session.scalar(
                select(ResourceDefinition)
                .where(
                    ResourceDefinition.guild_id == interaction.guild_id,
                    ResourceDefinition.name == resource_name,
                    ResourceDefinition.enabled.is_(True),
                )
                .with_for_update()
            )
            if not resource:
                await interaction.response.send_message(
                    "Активный ресурс не найден.", ephemeral=True
                )
                return
            resource.enabled = False
            productions = (
                await session.scalars(
                    select(ResourceProduction)
                    .where(ResourceProduction.resource_id == resource.id)
                    .with_for_update()
                )
            ).all()
            for production in productions:
                production.enabled = False
            session.add(
                AuditLog(
                    guild_id=interaction.guild_id,
                    actor_id=interaction.user.id,
                    action="resource_disable",
                    target_type="resource",
                    target_id=str(resource.id),
                    old_value="enabled",
                    new_value="disabled",
                    reason=reason[:1000],
                )
            )
            await session.commit()
        await interaction.response.send_message(
            f"Ресурс «{resource_name}» отключён; история сохранена.", ephemeral=True
        )

    @resource_disable.autocomplete("resource_name")
    async def resource_disable_autocomplete(interaction: discord.Interaction, current: str):
        return await _resource_choices(interaction, current, enabled=True)

    @bot.tree.command(
        name="resource-enable", description="Повторно включить ранее отключённый ресурс"
    )
    @app_commands.describe(resource_name="Отключённый ресурс из списка автодополнения")
    @admin_check(bot)
    async def resource_enable(interaction: discord.Interaction, resource_name: str):
        from vpi_bot.models import AuditLog, ResourceDefinition

        async with bot.db() as session:
            resource = await session.scalar(
                select(ResourceDefinition)
                .where(
                    ResourceDefinition.guild_id == interaction.guild_id,
                    ResourceDefinition.name == resource_name,
                )
                .with_for_update()
            )
            if not resource:
                await interaction.response.send_message("Ресурс не найден.", ephemeral=True)
                return
            old_enabled = resource.enabled
            resource.enabled = True
            session.add(
                AuditLog(
                    guild_id=interaction.guild_id,
                    actor_id=interaction.user.id,
                    action="resource_enable",
                    target_type="resource",
                    target_id=str(resource.id),
                    old_value=str(old_enabled),
                    new_value="True",
                )
            )
            await session.commit()
        await interaction.response.send_message(
            f"Ресурс «{resource_name}» включён. Настройте добычу заново при необходимости.",
            ephemeral=True,
        )

    @resource_enable.autocomplete("resource_name")
    async def resource_enable_autocomplete(interaction: discord.Interaction, current: str):
        return await _resource_choices(interaction, current, enabled=False)

    @bot.tree.command(name="production-set", description="Настроить добычу ресурса государством")
    @app_commands.describe(
        country_id="ID государства из /countries",
        resource_name="Активный ресурс, который будет производиться",
        amount_per_tick="Сколько единиц ресурса производить за один цикл",
        interval_hours="Интервал цикла в часах; пусто — системное значение",
    )
    @admin_check(bot)
    async def production_set(
        interaction: discord.Interaction,
        country_id: int,
        resource_name: str,
        amount_per_tick: app_commands.Range[float, 0.001, 1000000000.0],
        interval_hours: app_commands.Range[int, 1, 720] | None = None,
    ):
        from vpi_bot.models import AuditLog, ResourceDefinition, ResourceProduction

        interval_hours = interval_hours or bot.settings.resource_tick_hours
        async with bot.db() as session:
            country = await session.scalar(
                select(Country).where(
                    Country.id == country_id,
                    Country.guild_id == interaction.guild_id,
                    Country.is_active.is_(True),
                )
            )
            resource = await session.scalar(
                select(ResourceDefinition).where(
                    ResourceDefinition.guild_id == interaction.guild_id,
                    ResourceDefinition.name == resource_name,
                    ResourceDefinition.enabled.is_(True),
                )
            )
            if not country or not resource:
                await interaction.response.send_message(
                    "Государство или ресурс не найден.", ephemeral=True
                )
                return
            row = await session.scalar(
                select(ResourceProduction)
                .where(
                    ResourceProduction.country_id == country.id,
                    ResourceProduction.resource_id == resource.id,
                )
                .with_for_update()
            )
            old = (
                {
                    "amount_per_tick": str(row.amount_per_tick),
                    "interval_hours": row.interval_hours,
                    "enabled": row.enabled,
                }
                if row
                else None
            )
            if row is None:
                row = ResourceProduction(country_id=country.id, resource_id=resource.id)
                session.add(row)
            row.amount_per_tick, row.interval_hours, row.enabled = (
                amount_per_tick,
                interval_hours,
                True,
            )
            row.last_tick_at = None
            new = {
                "amount_per_tick": str(row.amount_per_tick),
                "interval_hours": row.interval_hours,
                "enabled": row.enabled,
            }
            await session.flush()
            session.add(
                AuditLog(
                    guild_id=interaction.guild_id,
                    actor_id=interaction.user.id,
                    action="production_set",
                    target_type="production",
                    target_id=str(row.id),
                    old_value=json.dumps(old),
                    new_value=json.dumps(new),
                )
            )
            await session.commit()
        await interaction.response.send_message(
            f"Добыча настроена: **{country.name}** получает {amount_per_tick:g} {resource.unit} «{resource.name}» раз в {interval_hours} ч.",
            ephemeral=True,
        )

    @production_set.autocomplete("resource_name")
    async def production_resource_autocomplete(interaction: discord.Interaction, current: str):
        return await _resource_choices(interaction, current, enabled=True)

    @production_set.autocomplete("country_id")
    async def production_country_autocomplete(interaction: discord.Interaction, current: str):
        return await country_choices(interaction, current)
