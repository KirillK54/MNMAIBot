import json
import logging
import math
from decimal import Decimal

import discord
from discord import app_commands
from sqlalchemy import select

from vpi_bot.commands.autocomplete import country_choices
from vpi_bot.epoch_service import refresh_country_epoch_cards
from vpi_bot.models import Country, EconomicStats, Epoch, GuildConfig, MilitaryStats
from vpi_bot.security import admin_check

log = logging.getLogger(__name__)


def _parse_epoch_starting_stats(raw: str) -> dict | None:
    if not raw.strip():
        return None
    try:
        template = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("Стартовые показатели должны быть корректным JSON.") from exc
    if not isinstance(template, dict) or set(template) - {"economy", "military", "resources"}:
        raise ValueError("В JSON допустимы только разделы economy, military и resources.")

    def numeric_mapping(
        value,
        allowed: set[str],
        section: str,
        integer_fields: set[str] | None = None,
    ):
        if integer_fields is None:
            integer_fields = set()
        if not isinstance(value, dict) or set(value) - allowed:
            raise ValueError(f"Проверьте поля раздела {section}.")
        cleaned = {}
        for key, number in value.items():
            if (
                isinstance(number, bool)
                or not isinstance(number, int | float)
                or not math.isfinite(number)
                or (number < 0 and key != "growth")
                or (section == "military" and key in {"readiness", "supply"} and number > 100)
                or (key in integer_fields and not isinstance(number, int))
            ):
                raise ValueError(f"Недопустимое стартовое значение {section}.{key}.")
            cleaned[key] = number
        return cleaned

    economy_fields = {
        "treasury",
        "gdp",
        "income",
        "expenses",
        "inflation",
        "unemployment",
        "growth",
        "debt",
        "tax_revenue",
        "industry_level",
        "stability",
    }
    military_fields = {
        "personnel",
        "budget",
        "technology_level",
        "readiness",
        "supply",
        "units",
        "equipment",
        "ammunition",
    }
    economy = numeric_mapping(
        template.get("economy", {}), economy_fields, "economy", {"industry_level"}
    )
    military_raw = template.get("military", {})
    military = (
        numeric_mapping(
            {
                key: value
                for key, value in military_raw.items()
                if key not in {"units", "equipment", "ammunition"}
            },
            military_fields - {"units", "equipment", "ammunition"},
            "military",
            {"personnel", "technology_level"},
        )
        if isinstance(military_raw, dict)
        else {}
    )
    if not isinstance(military_raw, dict):
        raise TypeError("Раздел military должен быть JSON-объектом.")
    for group in ("units", "equipment", "ammunition"):
        values = military_raw.get(group, {})
        if not isinstance(values, dict) or any(
            not isinstance(name, str)
            or not name.strip()
            or isinstance(amount, bool)
            or not isinstance(amount, int | float)
            or not math.isfinite(amount)
            or amount < 0
            for name, amount in values.items()
        ):
            raise ValueError(
                f"Раздел military.{group} должен содержать неотрицательные количества."
            )
        if values:
            military[group] = values
    resources = template.get("resources", {})
    if not isinstance(resources, dict) or any(
        not isinstance(name, str)
        or not name.strip()
        or isinstance(amount, bool)
        or not isinstance(amount, int | float)
        or not math.isfinite(amount)
        or amount < 0
        for name, amount in resources.items()
    ):
        raise ValueError("Раздел resources должен содержать неотрицательные количества.")
    result = {"economy": economy, "military": military, "resources": resources}
    return result


def register(bot) -> None:
    @bot.tree.command(
        name="epoch-set", description="Открыть меню выбора или задать эпоху по названию"
    )
    @app_commands.describe(
        name="Необязательно: укажите эпоху вручную; пусто — открыть графическое меню выбора",
        description="Для новой эпохи — описание; пусто — оставить описание шаблона",
        technologies="Доступные технологии через запятую; пусто — не менять шаблон",
        rules="Особые правила эпохи; пусто — не менять шаблон",
        starting_stats_json='JSON: {"economy":{"treasury":1000},"resources":{"Зерно":500}}',
    )
    @admin_check(bot)
    async def epoch_set(
        interaction: discord.Interaction,
        name: str | None = None,
        description: str = "",
        technologies: str = "",
        rules: str = "",
        starting_stats_json: str = "",
    ):
        from vpi_bot.models import AuditLog
        from vpi_bot.seed import seed_guild_defaults

        if not name or not name.strip():
            from vpi_bot.ui.epoch_picker import open_epoch_picker

            await open_epoch_picker(bot, interaction)
            return
        try:
            custom_starting_stats = _parse_epoch_starting_stats(starting_stats_json)
        except (ValueError, TypeError) as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        async with bot.db() as session:
            config = await session.get(GuildConfig, interaction.guild_id)
            if not config or not await bot.guild_config(interaction.guild_id):
                await interaction.response.send_message("Сервер не настроен.", ephemeral=True)
                return
            await seed_guild_defaults(session, interaction.guild_id)
            epoch = await session.scalar(
                select(Epoch).where(Epoch.guild_id == interaction.guild_id, Epoch.name == name)
            )
            if epoch is None:
                if not description.strip():
                    await interaction.response.send_message(
                        "Чтобы создать собственную эпоху, заполните её описание.", ephemeral=True
                    )
                    return
                epoch = Epoch(
                    guild_id=interaction.guild_id,
                    name=name[:120],
                    description=description[:3000],
                    rules=rules[:3000],
                    technologies=[
                        item.strip()[:120] for item in technologies.split(",") if item.strip()
                    ][:100],
                    starting_stats=custom_starting_stats or {},
                )
                session.add(epoch)
                await session.flush()
            else:
                if description.strip():
                    epoch.description = description[:3000]
                if technologies.strip():
                    epoch.technologies = [
                        item.strip()[:120] for item in technologies.split(",") if item.strip()
                    ][:100]
                if rules.strip():
                    epoch.rules = rules[:3000]
                if custom_starting_stats is not None:
                    epoch.starting_stats = custom_starting_stats
            old_name = config.epoch_name
            old = {"name": old_name}
            config.epoch_name = epoch.name
            new = {
                "name": epoch.name,
                "description": epoch.description,
                "rules": epoch.rules,
                "technologies": epoch.technologies,
                "starting_stats": epoch.starting_stats,
            }
            session.add(
                AuditLog(
                    guild_id=interaction.guild_id,
                    actor_id=interaction.user.id,
                    action="epoch_set",
                    target_type="guild",
                    target_id=str(interaction.guild_id),
                    old_value=json.dumps(old, ensure_ascii=False),
                    new_value=json.dumps(new, ensure_ascii=False),
                )
            )
            await session.commit()
        starting_note = (
            "Новые страны получат стартовый шаблон этой эпохи."
            if epoch.starting_stats
            else "Для этой эпохи применяются общие стартовые значения."
        )
        await interaction.response.send_message(
            f"Текущая эпоха: **{epoch.name}**. {starting_note} "
            "Показатели уже созданных стран не сброшены.",
            ephemeral=True,
        )
        updated, failed = await refresh_country_epoch_cards(bot, interaction.guild_id)
        if failed:
            await interaction.followup.send(
                f"Карточки обновлены: {updated}; не удалось обновить: {failed}. Проверьте права бота.",
                ephemeral=True,
            )

    @epoch_set.autocomplete("name")
    @admin_check(bot)
    async def epoch_autocomplete(interaction: discord.Interaction, current: str):
        from vpi_bot.seed import DEFAULT_EPOCHS

        guild_id = interaction.guild_id
        if guild_id is None:
            return []
        preset_rows = {
            preset.name: (preset.start_year, preset.end_year, preset.description)
            for preset in DEFAULT_EPOCHS
        }
        async with bot.db() as session:
            stored = (
                await session.execute(
                    select(Epoch.name, Epoch.start_year, Epoch.end_year, Epoch.description)
                    .where(Epoch.guild_id == guild_id)
                    .order_by(Epoch.start_year)
                )
            ).all()
        rows = dict(preset_rows)
        for era_name, start, end, description in stored:
            rows.setdefault(era_name, (start, end, description))
        query = current.strip().casefold()
        choices = []
        for era_name, (start, end, description) in rows.items():
            if query and query not in era_name.casefold() and query not in description.casefold():
                continue
            start_label = (
                f"{abs(start)} до н. э."
                if start is not None and start < 0
                else f"{start} н. э."
                if start is not None
                else "?"
            )
            end_label = (
                f"{abs(end)} до н. э."
                if end is not None and end < 0
                else f"{end} н. э."
                if end is not None
                else "далее"
            )
            years = f"{start_label}–{end_label}"
            label = f"{era_name} · {years} · {description}"[:100]
            choices.append(app_commands.Choice(name=label, value=era_name))
        return choices[:25]

    @bot.tree.command(name="epoch-menu", description="Открыть графический выбор эпохи")
    @admin_check(bot)
    async def epoch_menu(interaction: discord.Interaction):
        from vpi_bot.ui.epoch_picker import open_epoch_picker

        await open_epoch_picker(bot, interaction)

    @bot.tree.command(name="lore-add", description="Добавить запись в лор государства")
    @app_commands.describe(
        country_id="ID государства из /countries",
        title="Короткий заголовок записи",
        content="Текст факта или предыстории, которую должен учитывать судья",
        category="Раздел лора, например история, культура или политика",
    )
    @admin_check(bot)
    async def lore_add(
        interaction: discord.Interaction,
        country_id: int,
        title: str,
        content: str,
        category: str = "general",
    ):
        from vpi_bot.models import AuditLog, CountryLore

        async with bot.db() as session:
            country = await session.scalar(
                select(Country).where(
                    Country.id == country_id, Country.guild_id == interaction.guild_id
                )
            )
            if not country:
                await interaction.response.send_message("Государство не найдено.", ephemeral=True)
                return
            row = CountryLore(
                country_id=country.id,
                category=category[:80],
                title=title[:200],
                content=content[:8000],
            )
            session.add(row)
            await session.flush()
            session.add(
                AuditLog(
                    guild_id=interaction.guild_id,
                    actor_id=interaction.user.id,
                    action="lore_add",
                    target_type="lore",
                    target_id=str(row.id),
                    new_value=f"{row.category}|{row.title}|{row.content}"[:4000],
                )
            )
            await session.commit()
        await interaction.response.send_message(
            "Запись лора сохранена и будет учитываться судьёй.", ephemeral=True
        )

    @lore_add.autocomplete("country_id")
    @admin_check(bot)
    async def lore_country_autocomplete(interaction: discord.Interaction, current: str):
        return await country_choices(interaction, current)

    @bot.tree.command(
        name="industry-set", description="Добавить или настроить действующее предприятие"
    )
    @app_commands.describe(
        country_id="ID государства из /countries",
        name="Название предприятия; повторное имя обновит его рецепт",
        facility_type="Краткий тип производства, например металлургический завод",
        output_json='Выпуск за цикл, JSON: {"Сталь": 3}; используйте названия из /resource-catalog',
        input_json='Расход за цикл в JSON: {"Железо": 5}; пусто — без сырья',
        operating_cost="Расход казны за один производственный цикл",
        interval_hours="Длительность цикла в часах; пусто — системное значение",
    )
    @admin_check(bot)
    async def industry_set(
        interaction: discord.Interaction,
        country_id: int,
        name: str,
        facility_type: str,
        output_json: str,
        input_json: str = "{}",
        operating_cost: app_commands.Range[float, 0.0, 1000000000.0] = 0.0,
        interval_hours: app_commands.Range[int, 1, 720] | None = None,
    ):
        from vpi_bot.models import AuditLog, IndustryFacility, ResourceDefinition

        interval_hours = interval_hours or bot.settings.resource_tick_hours
        try:
            outputs, inputs = json.loads(output_json), json.loads(input_json)
            if not isinstance(outputs, dict) or not outputs or not isinstance(inputs, dict):
                raise ValueError
            if any(
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value <= 0
                or value > 1_000_000
                for value in outputs.values()
            ):
                raise ValueError
            if any(
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value <= 0
                or value > 1_000_000
                for value in inputs.values()
            ):
                raise ValueError
        except (ValueError, TypeError, json.JSONDecodeError):
            await interaction.response.send_message(
                'Введите JSON-объекты ресурсов, например `{"Сталь": 3}` и `{"Железо": 5}`.',
                ephemeral=True,
            )
            return
        async with bot.db() as session:
            country = await session.scalar(
                select(Country).where(
                    Country.id == country_id,
                    Country.guild_id == interaction.guild_id,
                    Country.is_active.is_(True),
                )
            )
            if not country:
                await interaction.response.send_message("Государство не найдено.", ephemeral=True)
                return
            names = set(outputs) | set(inputs)
            defined = set(
                (
                    await session.scalars(
                        select(ResourceDefinition.name).where(
                            ResourceDefinition.guild_id == interaction.guild_id,
                            ResourceDefinition.name.in_(names),
                            ResourceDefinition.enabled.is_(True),
                        )
                    )
                ).all()
            )
            if names != defined:
                await interaction.response.send_message(
                    "В рецепте есть ресурс, не зарегистрированный через /resource-add.",
                    ephemeral=True,
                )
                return
            row = await session.scalar(
                select(IndustryFacility)
                .where(
                    IndustryFacility.country_id == country.id, IndustryFacility.name == name[:120]
                )
                .with_for_update()
            )
            old = (
                {
                    "type": row.facility_type,
                    "outputs": row.production_per_tick,
                    "inputs": row.input_per_tick,
                    "cost": str(row.operating_cost),
                    "interval_hours": row.interval_hours,
                }
                if row
                else None
            )
            if row is None:
                row = IndustryFacility(
                    country_id=country.id, name=name[:120], facility_type=facility_type[:80]
                )
                session.add(row)
            row.facility_type = facility_type[:80]
            row.production_per_tick, row.input_per_tick = outputs, inputs
            row.operating_cost, row.interval_hours = operating_cost, interval_hours
            row.status, row.last_tick_at = "active", None
            new = {
                "type": row.facility_type,
                "outputs": row.production_per_tick,
                "inputs": row.input_per_tick,
                "cost": str(row.operating_cost),
                "interval_hours": row.interval_hours,
            }
            session.add(
                AuditLog(
                    guild_id=interaction.guild_id,
                    actor_id=interaction.user.id,
                    action="industry_set",
                    target_type="industry",
                    target_id=str(row.id),
                    old_value=json.dumps(old, ensure_ascii=False) if old else None,
                    new_value=json.dumps(new, ensure_ascii=False),
                )
            )
            await session.commit()
        await interaction.response.send_message(
            f"Предприятие **{name}** настроено; цикл производства выполняется автоматически.",
            ephemeral=True,
        )

    @industry_set.autocomplete("country_id")
    @admin_check(bot)
    async def industry_country_autocomplete(interaction: discord.Interaction, current: str):
        return await country_choices(interaction, current)

    @bot.tree.command(
        name="military-adjust", description="Увеличить или уменьшить военный показатель"
    )
    @admin_check(bot)
    @app_commands.describe(
        country_id="ID государства из /countries",
        section="Выберите показатели, подразделения, технику или боеприпасы",
        field="Показатель (personnel, budget, technology_level, readiness, supply) либо имя элемента",
        delta="Изменение количества: положительное добавить, отрицательное убрать",
        reason="Причина изменения для журнала аудита",
    )
    @app_commands.choices(
        section=[
            app_commands.Choice(name="Показатели", value="metric"),
            app_commands.Choice(name="Подразделения", value="units"),
            app_commands.Choice(name="Техника", value="equipment"),
            app_commands.Choice(name="Боеприпасы", value="ammunition"),
        ]
    )
    async def military_adjust(
        interaction: discord.Interaction,
        country_id: int,
        section: app_commands.Choice[str],
        field: str,
        delta: app_commands.Range[float, -1000000000.0, 1000000000.0],
        reason: str,
    ):
        from vpi_bot.military import adjust_military
        from vpi_bot.models import AuditLog, EconomicOperation

        async with bot.db() as session:
            try:
                (
                    _country,
                    old,
                    new,
                    treasury_delta,
                    treasury_before,
                    treasury_after,
                ) = await adjust_military(
                    session,
                    guild_id=interaction.guild_id,
                    country_id=country_id,
                    section=section.value,
                    key=field,
                    raw_delta=delta,
                )
            except ValueError as exc:
                await interaction.response.send_message(str(exc), ephemeral=True)
                return
            session.add(
                AuditLog(
                    guild_id=interaction.guild_id,
                    actor_id=interaction.user.id,
                    action="military_adjust",
                    target_type="country",
                    target_id=str(country_id),
                    old_value=f"{section.value}.{field}={old}",
                    new_value=f"{section.value}.{field}={new}",
                    reason=reason[:1000],
                )
            )
            if treasury_delta:
                session.add(
                    EconomicOperation(
                        country_id=country_id,
                        category="treasury",
                        source="military_adjust",
                        source_id=str(interaction.id),
                        delta=treasury_delta,
                        before_value=treasury_before,
                        after_value=treasury_after,
                    )
                )
            await session.commit()
        await interaction.response.send_message(
            f"{section.name} / {field}: {old} → {new}. Изменение записано в журнал.",
            ephemeral=True,
        )

    @military_adjust.autocomplete("country_id")
    @admin_check(bot)
    async def military_adjust_country_autocomplete(interaction: discord.Interaction, current: str):
        return await country_choices(interaction, current)

    @bot.tree.command(name="military-set", description="Установить военную статистику государства")
    @app_commands.describe(
        country_id="ID государства из /countries",
        personnel="Общая численность личного состава",
        budget="Военный бюджет; сумма списывается из казны или возвращается в неё",
        technology_level="Текущий уровень военных технологий",
        readiness="Готовность армии в процентах от 0 до 100",
        supply="Снабжение армии в процентах от 0 до 100",
        units_json='Подразделения и численность, например {"пехота": 5000}',
        equipment_json='Техника и количество, например {"танки": 120}',
        ammunition_json='Боеприпасы и количество, например {"снаряды": 9000}',
    )
    @admin_check(bot)
    async def military_set(
        interaction: discord.Interaction,
        country_id: int,
        personnel: app_commands.Range[int, 0, 100000000],
        budget: app_commands.Range[float, 0.0, 1000000000000.0],
        technology_level: app_commands.Range[int, 0, 1000],
        readiness: app_commands.Range[float, 0.0, 100.0],
        supply: app_commands.Range[float, 0.0, 100.0],
        units_json: str = "{}",
        equipment_json: str = "{}",
        ammunition_json: str = "{}",
    ):
        from vpi_bot.models import AuditLog, EconomicOperation

        try:
            units, equipment, ammunition = map(
                json.loads, (units_json, equipment_json, ammunition_json)
            )
            groups = (units, equipment, ammunition)
            if not all(isinstance(value, dict) for value in groups):
                raise ValueError
            for group in groups:
                if any(
                    not isinstance(key, str)
                    or not key
                    or len(key) > 100
                    or isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not math.isfinite(value)
                    or value < 0
                    or value > 1_000_000_000
                    for key, value in group.items()
                ):
                    raise ValueError
        except (ValueError, TypeError, json.JSONDecodeError):
            await interaction.response.send_message(
                "Подразделения, техника и боеприпасы должны быть JSON-объектами.", ephemeral=True
            )
            return
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
            if not country:
                await interaction.response.send_message(
                    "Действующее государство не найдено.", ephemeral=True
                )
                return
            stats = await session.scalar(
                select(EconomicStats)
                .where(EconomicStats.country_id == country.id)
                .with_for_update()
            )
            if not stats:
                await interaction.response.send_message(
                    "Экономическая статистика не найдена.", ephemeral=True
                )
                return
            army = await session.scalar(
                select(MilitaryStats)
                .where(MilitaryStats.country_id == country.id)
                .with_for_update()
            )
            old = (
                {
                    "personnel": army.personnel,
                    "budget": str(army.budget),
                    "technology_level": army.technology_level,
                    "readiness": str(army.readiness),
                    "supply": str(army.supply),
                    "units": army.units,
                    "equipment": army.equipment,
                    "ammunition": army.ammunition,
                }
                if army
                else None
            )
            if army is None:
                army = MilitaryStats(country_id=country.id)
                session.add(army)
            budget = Decimal(str(budget)).quantize(Decimal("0.01"))
            budget_before = Decimal(army.budget)
            treasury_before = Decimal(stats.treasury)
            treasury_delta = budget_before - budget
            treasury_after = treasury_before + treasury_delta
            if treasury_after < 0 or treasury_after > Decimal("999999999999999999.99"):
                await interaction.response.send_message(
                    "Недостаточно казны для установки военного бюджета.", ephemeral=True
                )
                return
            stats.treasury = treasury_after
            army.personnel, army.budget, army.technology_level = personnel, budget, technology_level
            army.readiness, army.supply = readiness, supply
            army.units, army.equipment, army.ammunition = units, equipment, ammunition
            new = {
                "personnel": army.personnel,
                "budget": str(army.budget),
                "technology_level": army.technology_level,
                "readiness": str(army.readiness),
                "supply": str(army.supply),
                "units": army.units,
                "equipment": army.equipment,
                "ammunition": army.ammunition,
            }
            if treasury_delta:
                session.add(
                    EconomicOperation(
                        country_id=country.id,
                        category="treasury",
                        source="military_set",
                        source_id=str(interaction.id),
                        delta=treasury_delta,
                        before_value=treasury_before,
                        after_value=treasury_after,
                    )
                )
            session.add(
                AuditLog(
                    guild_id=interaction.guild_id,
                    actor_id=interaction.user.id,
                    action="military_set",
                    target_type="country",
                    target_id=str(country.id),
                    old_value=json.dumps(old, ensure_ascii=False) if old else None,
                    new_value=json.dumps(new, ensure_ascii=False),
                )
            )
            await session.commit()
        await interaction.response.send_message(
            f"Военная статистика государства **{country.name}** обновлена.", ephemeral=True
        )

    @military_set.autocomplete("country_id")
    @admin_check(bot)
    async def military_set_country_autocomplete(interaction: discord.Interaction, current: str):
        return await country_choices(interaction, current)

    @bot.tree.command(
        name="technology-set", description="Добавить или обновить технологию государства"
    )
    @app_commands.describe(
        country_id="ID государства из /countries",
        name="Название уже освоенной технологии",
        level="Уровень технологии от 1 до 1000",
        description="Краткое описание эффекта технологии",
    )
    @admin_check(bot)
    async def technology_set(
        interaction: discord.Interaction,
        country_id: int,
        name: str,
        level: app_commands.Range[int, 1, 1000] = 1,
        description: str = "",
    ):
        from vpi_bot.models import AuditLog, CountryTechnology

        async with bot.db() as session:
            country = await session.scalar(
                select(Country).where(
                    Country.id == country_id, Country.guild_id == interaction.guild_id
                )
            )
            if not country:
                await interaction.response.send_message("Государство не найдено.", ephemeral=True)
                return
            technology = await session.scalar(
                select(CountryTechnology).where(
                    CountryTechnology.country_id == country.id, CountryTechnology.name == name[:160]
                )
            )
            old = (
                {
                    "level": technology.level,
                    "description": technology.description,
                    "status": technology.status,
                }
                if technology
                else None
            )
            if technology is None:
                technology = CountryTechnology(country_id=country.id, name=name[:160])
                session.add(technology)
            technology.level, technology.description, technology.status = (
                level,
                description[:2000],
                "researched",
            )
            new = {
                "level": technology.level,
                "description": technology.description,
                "status": technology.status,
            }
            session.add(
                AuditLog(
                    guild_id=interaction.guild_id,
                    actor_id=interaction.user.id,
                    action="technology_set",
                    target_type="technology",
                    target_id=str(technology.id),
                    old_value=json.dumps(old, ensure_ascii=False) if old else None,
                    new_value=json.dumps(new, ensure_ascii=False),
                )
            )
            await session.commit()
        await interaction.response.send_message(f"Технология **{name}** сохранена.", ephemeral=True)

    @technology_set.autocomplete("country_id")
    @admin_check(bot)
    async def technology_country_autocomplete(interaction: discord.Interaction, current: str):
        return await country_choices(interaction, current)
