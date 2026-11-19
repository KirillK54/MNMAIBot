from decimal import Decimal, InvalidOperation

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from vpi_bot.models import (
    ConstructionProject,
    Country,
    CountryResource,
    CountryTechnology,
    EconomicStats,
    HistoricalEvent,
    IndustryFacility,
    MilitaryStats,
    ResearchProject,
    ResourceDefinition,
    ResourceOperation,
    StatChange,
    Verdict,
)

ALLOWED_METRICS = {
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
MONEY_METRICS = {"treasury", "gdp", "income", "expenses", "debt", "tax_revenue"}
BOUNDED_METRICS = {
    "inflation": (Decimal(0), Decimal(1000)),
    "unemployment": (Decimal(0), Decimal(100)),
    "growth": (Decimal(-100), Decimal(100)),
    "industry_level": (Decimal(0), Decimal(100000)),
    "stability": (Decimal(0), Decimal(100)),
}


class ChangeRejected(ValueError):
    pass


def _decimal(value: object) -> Decimal:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ChangeRejected("Изменение не является числом") from exc
    if not number.is_finite():
        raise ChangeRejected("Неконечные значения запрещены")
    return number


async def advance_construction_projects(
    session: AsyncSession, country_id: int, verdict_id: int
) -> list[str]:
    """Advance previously existing projects by one accepted action and activate completed facilities."""
    projects = (
        await session.scalars(
            select(ConstructionProject)
            .where(
                ConstructionProject.country_id == country_id,
                ConstructionProject.status == "building",
            )
            .order_by(ConstructionProject.id)
            .with_for_update()
        )
    ).all()
    completed = []
    for project in projects:
        before = project.completed_turns
        project.completed_turns += 1
        session.add(
            StatChange(
                verdict_id=verdict_id,
                country_id=country_id,
                change_key=f"project_progress:{project.id}",
                before_value=Decimal(before),
                delta=Decimal(1),
            )
        )
        if project.completed_turns < project.duration_turns:
            continue
        project.status = "completed"
        session.add(
            IndustryFacility(
                country_id=project.country_id,
                name=project.name,
                facility_type=project.project_type,
                level=1,
                production_per_tick=project.production_per_tick,
                input_per_tick=project.input_per_tick,
                source_project_id=project.id,
                operating_cost=project.operating_cost,
                interval_hours=project.interval_hours,
                status="active",
            )
        )
        session.add(
            HistoricalEvent(
                country_id=country_id,
                title=f"Завершён проект: {project.name}",
                content=f"Построено предприятие типа «{project.project_type}».",
                importance=7,
                tags=["construction", project.project_type],
            )
        )
        completed.append(project.name)
    if completed:
        await session.flush()
        from vpi_bot.lore import refresh_event_summary

        await refresh_event_summary(session, country_id)
    return completed


async def advance_research_projects(
    session: AsyncSession, country_id: int, verdict_id: int
) -> list[str]:
    projects = (
        await session.scalars(
            select(ResearchProject)
            .where(
                ResearchProject.country_id == country_id, ResearchProject.status == "researching"
            )
            .order_by(ResearchProject.id)
            .with_for_update()
        )
    ).all()
    completed = []
    for project in projects:
        before = project.completed_turns
        project.completed_turns += 1
        session.add(
            StatChange(
                verdict_id=verdict_id,
                country_id=country_id,
                change_key=f"research_progress:{project.id}",
                before_value=Decimal(before),
                delta=Decimal(1),
            )
        )
        if project.completed_turns < project.duration_turns:
            continue
        project.status = "completed"
        technology = await session.scalar(
            select(CountryTechnology)
            .where(
                CountryTechnology.country_id == country_id,
                CountryTechnology.name == project.technology_name,
            )
            .with_for_update()
        )
        if technology is None:
            technology = CountryTechnology(
                country_id=country_id,
                name=project.technology_name,
                level=project.target_level,
                description=project.description,
                status="researched",
            )
            session.add(technology)
        else:
            technology.level = max(technology.level, project.target_level)
            technology.description = project.description
            technology.status = "researched"
        session.add(
            HistoricalEvent(
                country_id=country_id,
                title=f"Завершено исследование: {project.technology_name}",
                content=project.description,
                importance=8,
                tags=["research", project.technology_name],
            )
        )
        completed.append(project.technology_name)
    if completed:
        await session.flush()
        from vpi_bot.lore import refresh_event_summary

        await refresh_event_summary(session, country_id)
    return completed


async def _apply_resource_delta(
    session: AsyncSession,
    country: Country,
    verdict: Verdict,
    name: str,
    delta: Decimal,
    *,
    source: str = "verdict",
) -> None:
    resource = await session.scalar(
        select(ResourceDefinition).where(
            ResourceDefinition.guild_id == country.guild_id,
            ResourceDefinition.name == name,
            ResourceDefinition.enabled.is_(True),
        )
    )
    if resource is None:
        raise ChangeRejected(f"Ресурс «{name}» не зарегистрирован на сервере")
    inventory = await session.scalar(
        select(CountryResource)
        .where(CountryResource.country_id == country.id, CountryResource.resource_id == resource.id)
        .with_for_update()
    )
    if inventory is None:
        if delta < 0:
            raise ChangeRejected(f"На складе нет ресурса «{name}»")
        inventory = CountryResource(
            country_id=country.id, resource_id=resource.id, quantity=Decimal(0)
        )
        session.add(inventory)
        await session.flush()
    if abs(delta) > Decimal(1000000):
        raise ChangeRejected(f"Изменение ресурса «{name}» превышает лимит одного хода")
    before = _decimal(inventory.quantity)
    after = before + delta
    if after < 0:
        raise ChangeRejected(f"Недостаточно ресурса «{name}»")
    if after > Decimal("99999999999999999.999"):
        raise ChangeRejected(f"Склад переполнен ресурсом «{name}»")
    inventory.quantity = after
    session.add_all(
        [
            ResourceOperation(
                country_id=country.id,
                resource_id=resource.id,
                source=source,
                source_id=str(verdict.id),
                delta=delta,
                before_value=before,
                after_value=after,
            ),
            StatChange(
                verdict_id=verdict.id,
                country_id=country.id,
                change_key=f"resource:{resource.id}",
                before_value=before,
                delta=delta,
            ),
        ]
    )


async def apply_changes(
    session: AsyncSession, country: Country, verdict: Verdict, proposed: dict
) -> dict[str, object]:
    """Validate and apply a verdict's deltas atomically; caller owns transaction."""
    country = await session.scalar(
        select(Country)
        .where(Country.id == country.id, Country.is_active.is_(True))
        .with_for_update()
    )
    if country is None:
        raise ChangeRejected("Государство архивировано или не найдено")
    stats = await session.scalar(
        select(EconomicStats).where(EconomicStats.country_id == country.id).with_for_update()
    )
    if stats is None:
        raise ChangeRejected("У государства отсутствует экономическая статистика")
    applied: dict[str, object] = {"metrics": {}, "resources": {}, "projects": [], "research": []}
    if verdict.status in {"success", "partial_success"}:
        applied["completed_projects"] = await advance_construction_projects(
            session, country.id, verdict.id
        )
        applied["completed_research"] = await advance_research_projects(
            session, country.id, verdict.id
        )
    else:
        applied["completed_projects"] = []
        applied["completed_research"] = []
    for key, raw_delta in proposed.get("metrics", {}).items():
        if key not in ALLOWED_METRICS:
            raise ChangeRejected(f"Неизвестный экономический показатель: {key}")
        delta = _decimal(raw_delta)
        if key in MONEY_METRICS:
            delta = delta.quantize(Decimal("0.01"))
        elif key in BOUNDED_METRICS:
            delta = delta.quantize(Decimal("0.001"))
        if key == "industry_level" and delta != delta.to_integral_value():
            raise ChangeRejected("Изменение уровня промышленности должно быть целым числом")
        before = _decimal(getattr(stats, key))
        change_cap = max(Decimal(100000), abs(_decimal(stats.gdp)) * Decimal("0.25"))
        if abs(delta) > change_cap:
            raise ChangeRejected(f"Изменение {key} превышает допустимый лимит за один ход")
        after = before + delta
        if after < 0 and key != "growth":
            raise ChangeRejected(f"Недостаточно средств или значения показателя {key}")
        if key in BOUNDED_METRICS:
            low, high = BOUNDED_METRICS[key]
            if not low <= after <= high:
                raise ChangeRejected(f"Значение {key} выходит за допустимый диапазон")
        setattr(stats, key, int(after) if key == "industry_level" else after)
        session.add(
            StatChange(
                verdict_id=verdict.id,
                country_id=country.id,
                change_key=f"metric:{key}",
                before_value=before,
                delta=delta,
            )
        )
        applied["metrics"][key] = float(delta)

    for name, raw_delta in proposed.get("resources", {}).items():
        delta = _decimal(raw_delta)
        if delta == 0:
            continue
        await _apply_resource_delta(session, country, verdict, name, delta)
        applied["resources"][name] = float(delta)

    military_changes = proposed.get("military") or {}
    military_has_changes = any(
        military_changes.get(key) for key in ("metrics", "units", "equipment", "ammunition")
    )
    if military_has_changes:
        army = await session.scalar(
            select(MilitaryStats).where(MilitaryStats.country_id == country.id).with_for_update()
        )
        if army is None:
            if any(
                _decimal(value) < 0
                for group in military_changes.values()
                for value in group.values()
            ):
                raise ChangeRejected("Военная статистика отсутствует для списания")
            army = MilitaryStats(country_id=country.id)
            session.add(army)
            await session.flush()
        army_result = {"metrics": {}, "units": {}, "equipment": {}, "ammunition": {}}
        allowed_army_metrics = {"personnel", "budget", "technology_level", "readiness", "supply"}
        bounded_army = {
            "readiness": (Decimal(0), Decimal(100)),
            "supply": (Decimal(0), Decimal(100)),
            "technology_level": (Decimal(0), Decimal(1000)),
            "personnel": (Decimal(0), Decimal(100000000)),
        }
        for key, raw_delta in military_changes.get("metrics", {}).items():
            if key not in allowed_army_metrics:
                raise ChangeRejected(f"Неизвестный военный показатель: {key}")
            delta = _decimal(raw_delta)
            if key in {"budget", "readiness", "supply"}:
                delta = delta.quantize(Decimal("0.01"))
            before = _decimal(getattr(army, key))
            if abs(delta) > max(Decimal(100000), abs(_decimal(stats.gdp)) * Decimal("0.25")):
                raise ChangeRejected(
                    f"Изменение военного показателя {key} превышает лимит одного хода"
                )
            after = before + delta
            low, high = bounded_army.get(key, (Decimal(0), Decimal(1000000000000)))
            if not low <= after <= high:
                raise ChangeRejected(f"Военный показатель {key} вне допустимого диапазона")
            if key in {"personnel", "technology_level"} and after != after.to_integral_value():
                raise ChangeRejected(f"Показатель {key} должен быть целым числом")
            if key == "budget":
                treasury_delta = -delta
                treasury_before = _decimal(stats.treasury)
                if treasury_before + treasury_delta < 0:
                    raise ChangeRejected("Недостаточно казны для изменения военного бюджета")
                stats.treasury = treasury_before + treasury_delta
                session.add(
                    StatChange(
                        verdict_id=verdict.id,
                        country_id=country.id,
                        change_key="metric:treasury",
                        before_value=treasury_before,
                        delta=treasury_delta,
                    )
                )
                applied["metrics"]["treasury"] = applied["metrics"].get("treasury", 0.0) + float(
                    treasury_delta
                )
            setattr(army, key, int(after) if key in {"personnel", "technology_level"} else after)
            session.add(
                StatChange(
                    verdict_id=verdict.id,
                    country_id=country.id,
                    change_key=f"military:metrics:{key}",
                    before_value=before,
                    delta=delta,
                )
            )
            army_result["metrics"][key] = float(delta)
        for field in ("units", "equipment", "ammunition"):
            values = dict(getattr(army, field) or {})
            for name, raw_delta in military_changes.get(field, {}).items():
                if not isinstance(name, str) or not name or len(name) > 100:
                    raise ChangeRejected("Некорректная категория военного имущества")
                delta = _decimal(raw_delta)
                before = _decimal(values.get(name, 0))
                after = before + delta
                if after < 0 or after > Decimal(1000000000):
                    raise ChangeRejected(f"Недостаточно запасов или превышен лимит для «{name}»")
                values[name] = float(after)
                session.add(
                    StatChange(
                        verdict_id=verdict.id,
                        country_id=country.id,
                        change_key=f"military:{field}:{name}",
                        before_value=before,
                        delta=delta,
                    )
                )
                army_result[field][name] = float(delta)
            setattr(army, field, values)
        applied["military"] = army_result
    else:
        applied["military"] = {"metrics": {}, "units": {}, "equipment": {}, "ammunition": {}}

    project_proposals = proposed.get("projects", [])
    if not isinstance(project_proposals, list) or len(project_proposals) > 3:
        raise ChangeRejected("За один ход можно начать не более трёх проектов")
    if project_proposals and verdict.status not in {"success", "partial_success"}:
        raise ChangeRejected(
            "Строительные проекты можно начинать только при успехе или частичном успехе"
        )
    active_count = (
        await session.scalar(
            select(func.count(ConstructionProject.id)).where(
                ConstructionProject.country_id == country.id,
                ConstructionProject.status == "building",
            )
        )
        or 0
    )
    if active_count + len(project_proposals) > 20:
        raise ChangeRejected("Превышен лимит параллельных строительных проектов")
    for proposal in project_proposals:
        name = str(proposal.get("name", "")).strip()
        facility_type = str(proposal.get("facility_type", "")).strip()
        if not name or len(name) > 120 or not facility_type or len(facility_type) > 80:
            raise ChangeRejected("Некорректное название строительного проекта")
        cost = _decimal(proposal.get("cost", 0)).quantize(Decimal("0.01"))
        operating_cost = _decimal(proposal.get("operating_cost", 0)).quantize(Decimal("0.01"))
        cap = max(Decimal(100000), abs(_decimal(stats.gdp)) * Decimal("0.25"))
        if cost < 0 or cost > cap or operating_cost < 0 or operating_cost > Decimal(1000000000):
            raise ChangeRejected("Стоимость проекта превышает допустимый лимит")
        if stats.treasury < cost:
            raise ChangeRejected("Недостаточно казны для строительного проекта")
        duration = int(proposal.get("duration_turns", 1))
        interval_hours = int(proposal.get("interval_hours", 4))
        if not 1 <= duration <= 100 or not 1 <= interval_hours <= 720:
            raise ChangeRejected("Срок или интервал проекта вне допустимого диапазона")
        required_technology = proposal.get("required_technology")
        if required_technology:
            technology = await session.scalar(
                select(CountryTechnology.id).where(
                    CountryTechnology.country_id == country.id,
                    CountryTechnology.name == required_technology,
                    CountryTechnology.status == "researched",
                )
            )
            if technology is None:
                raise ChangeRejected(f"Не исследована технология «{required_technology}»")
        recipe_groups = {
            key: proposal.get(key, {})
            for key in ("resource_costs", "production_per_tick", "input_per_tick")
        }
        for group_name, group in recipe_groups.items():
            if not isinstance(group, dict) or len(group) > 20:
                raise ChangeRejected(f"Некорректная рецептура проекта: {group_name}")
            for resource_name, raw_amount in group.items():
                amount = _decimal(raw_amount)
                if (
                    not isinstance(resource_name, str)
                    or not resource_name
                    or len(resource_name) > 100
                    or amount <= 0
                    or amount > Decimal(1000000)
                ):
                    raise ChangeRejected(f"Недопустимый ресурс в проекте: {resource_name}")
        if not recipe_groups["production_per_tick"]:
            raise ChangeRejected("У проекта должен быть задан выпуск ресурсов")
        resource_names = sorted(set().union(*(set(group) for group in recipe_groups.values())))
        definitions = (
            (
                await session.scalars(
                    select(ResourceDefinition).where(
                        ResourceDefinition.guild_id == country.guild_id,
                        ResourceDefinition.name.in_(resource_names),
                        ResourceDefinition.enabled.is_(True),
                    )
                )
            ).all()
            if resource_names
            else []
        )
        if {row.name for row in definitions} != set(resource_names):
            raise ChangeRejected("В проекте указан незарегистрированный ресурс")
        for resource_name, raw_cost in sorted(recipe_groups["resource_costs"].items()):
            delta = -_decimal(raw_cost)
            await _apply_resource_delta(
                session, country, verdict, resource_name, delta, source="project_reserve"
            )
            applied["resources"][resource_name] = applied["resources"].get(
                resource_name, 0.0
            ) + float(delta)
        if cost:
            treasury_before = _decimal(stats.treasury)
            if treasury_before < cost:
                raise ChangeRejected("Недостаточно казны для строительного проекта")
            stats.treasury = treasury_before - cost
            session.add(
                StatChange(
                    verdict_id=verdict.id,
                    country_id=country.id,
                    change_key="metric:treasury",
                    before_value=treasury_before,
                    delta=-cost,
                )
            )
            applied["metrics"]["treasury"] = applied["metrics"].get("treasury", 0.0) - float(cost)
        project = ConstructionProject(
            source_verdict_id=verdict.id,
            country_id=country.id,
            project_type=facility_type,
            name=name,
            cost=cost,
            resource_costs=recipe_groups["resource_costs"],
            production_per_tick=recipe_groups["production_per_tick"],
            input_per_tick=recipe_groups["input_per_tick"],
            operating_cost=operating_cost,
            interval_hours=interval_hours,
            required_technology=required_technology,
            duration_turns=duration,
            status="building",
        )
        session.add(project)
        await session.flush()
        applied["projects"].append(
            {
                "id": project.id,
                "name": project.name,
                "duration_turns": project.duration_turns,
                "required_technology": project.required_technology,
            }
        )

    research_proposals = proposed.get("research", [])
    if not isinstance(research_proposals, list) or len(research_proposals) > 1:
        raise ChangeRejected("За один раз можно начать одно исследование")
    if research_proposals and verdict.status not in {"success", "partial_success"}:
        raise ChangeRejected("Исследование начинается только при успехе или частичном успехе")
    if research_proposals:
        active_research = await session.scalar(
            select(ResearchProject.id).where(
                ResearchProject.country_id == country.id,
                ResearchProject.status == "researching",
            )
        )
        if active_research:
            raise ChangeRejected("Сначала завершите текущее исследование")
    for proposal in research_proposals:
        technology_name = str(proposal.get("technology_name", "")).strip()
        description = str(proposal.get("description", "")).strip()
        duration = int(proposal.get("duration_turns", 0))
        if (
            not technology_name
            or len(technology_name) > 160
            or not description
            or len(description) > 2000
            or not 1 <= duration <= 5
        ):
            raise ChangeRejected("Некорректное название, описание или срок исследования")
        existing_technology = await session.scalar(
            select(CountryTechnology)
            .where(
                CountryTechnology.country_id == country.id,
                CountryTechnology.name == technology_name,
            )
            .with_for_update()
        )
        target_level = existing_technology.level + 1 if existing_technology else 1
        research = ResearchProject(
            source_verdict_id=verdict.id,
            country_id=country.id,
            technology_name=technology_name,
            target_level=target_level,
            description=description,
            duration_turns=duration,
            status="researching",
        )
        session.add(research)
        await session.flush()
        applied["research"].append(
            {
                "id": research.id,
                "technology_name": technology_name,
                "target_level": target_level,
                "duration_turns": duration,
            }
        )
    return applied


async def reverse_changes(session: AsyncSession, verdict: Verdict) -> None:
    """Reverse exactly this verdict's deltas, refusing to create negative balances."""
    if (verdict.applied_changes or {}).get("completed_projects"):
        raise ChangeRejected("Нельзя отменить вердикт: его ход завершил строительный проект")
    if (verdict.applied_changes or {}).get("completed_research"):
        raise ChangeRejected("Нельзя отменить вердикт: его ход завершил исследование")
    research_projects = (
        await session.scalars(
            select(ResearchProject)
            .where(ResearchProject.source_verdict_id == verdict.id)
            .with_for_update()
        )
    ).all()
    if any(
        project.status == "completed" or project.completed_turns > 0
        for project in research_projects
    ):
        raise ChangeRejected("Нельзя отменить вердикт: исследование уже продвинулось")
    projects = (
        await session.scalars(
            select(ConstructionProject)
            .where(ConstructionProject.source_verdict_id == verdict.id)
            .with_for_update()
        )
    ).all()
    if any(project.status == "completed" or project.completed_turns > 0 for project in projects):
        raise ChangeRejected("Нельзя отменить вердикт: строительный проект уже продвинулся")
    changes = (
        await session.scalars(
            select(StatChange)
            .where(StatChange.verdict_id == verdict.id)
            .order_by(StatChange.id.desc())
            .with_for_update()
        )
    ).all()
    for entry in changes:
        delta = -entry.delta
        if entry.change_key.startswith("research_progress:"):
            project_id = int(entry.change_key.split(":", 1)[1])
            project = await session.scalar(
                select(ResearchProject).where(ResearchProject.id == project_id).with_for_update()
            )
            if project is None or project.status != "researching":
                raise ChangeRejected("Нельзя безопасно отменить прогресс исследования")
            after = Decimal(project.completed_turns) + delta
            if after < 0 or after >= project.duration_turns or after != after.to_integral_value():
                raise ChangeRejected("Нельзя безопасно отменить прогресс исследования")
            project.completed_turns = int(after)
        elif entry.change_key.startswith("project_progress:"):
            project_id = int(entry.change_key.split(":", 1)[1])
            project = await session.scalar(
                select(ConstructionProject)
                .where(ConstructionProject.id == project_id)
                .with_for_update()
            )
            if project is None or project.status != "building":
                raise ChangeRejected("Нельзя безопасно отменить прогресс проекта")
            after = Decimal(project.completed_turns) + delta
            if after < 0 or after >= project.duration_turns or after != after.to_integral_value():
                raise ChangeRejected("Нельзя безопасно отменить прогресс проекта")
            project.completed_turns = int(after)
        elif entry.change_key.startswith("military:"):
            _, field, key = entry.change_key.split(":", 2)
            army = await session.scalar(
                select(MilitaryStats)
                .where(MilitaryStats.country_id == entry.country_id)
                .with_for_update()
            )
            if army is None:
                raise ChangeRejected("Нельзя отменить вердикт: военные данные отсутствуют")
            if field == "metrics":
                current = _decimal(getattr(army, key))
                after = current + delta
                bounds = {
                    "readiness": (Decimal(0), Decimal(100)),
                    "supply": (Decimal(0), Decimal(100)),
                    "technology_level": (Decimal(0), Decimal(1000)),
                    "personnel": (Decimal(0), Decimal(100000000)),
                }
                low, high = bounds.get(key, (Decimal(0), Decimal(1000000000000)))
                if not low <= after <= high or (
                    key in {"personnel", "technology_level"} and after != after.to_integral_value()
                ):
                    raise ChangeRejected(f"Нельзя безопасно отменить военный показатель {key}")
                setattr(
                    army, key, int(after) if key in {"personnel", "technology_level"} else after
                )
            elif field in {"units", "equipment", "ammunition"}:
                values = dict(getattr(army, field) or {})
                current = _decimal(values.get(key, 0))
                after = current + delta
                if after < 0:
                    raise ChangeRejected(f"Нельзя отменить вердикт: «{key}» уже израсходовано")
                values[key] = float(after)
                setattr(army, field, values)
            else:
                raise ChangeRejected("Неизвестный тип военного изменения")
        elif entry.change_key.startswith("metric:"):
            key = entry.change_key.split(":", 1)[1]
            stats = await session.scalar(
                select(EconomicStats)
                .where(EconomicStats.country_id == entry.country_id)
                .with_for_update()
            )
            current = _decimal(getattr(stats, key))
            after = current + delta
            if after < 0 and key != "growth":
                raise ChangeRejected(
                    f"Нельзя отменить вердикт: последующие операции используют {key}"
                )
            if key in BOUNDED_METRICS:
                low, high = BOUNDED_METRICS[key]
                if not low <= after <= high:
                    raise ChangeRejected(f"Нельзя безопасно отменить показатель {key}")
            if key == "industry_level" and after != after.to_integral_value():
                raise ChangeRejected("Нельзя безопасно отменить уровень промышленности")
            setattr(stats, key, int(after) if key == "industry_level" else after)
        else:
            resource_id = int(entry.change_key.split(":", 1)[1])
            inventory = await session.scalar(
                select(CountryResource)
                .where(
                    CountryResource.country_id == entry.country_id,
                    CountryResource.resource_id == resource_id,
                )
                .with_for_update()
            )
            current = _decimal(inventory.quantity if inventory else 0)
            if current + delta < 0:
                raise ChangeRejected("Нельзя отменить вердикт: ресурс уже израсходован")
            after = current + delta
            if inventory:
                inventory.quantity = after
            elif after > 0:
                session.add(
                    CountryResource(
                        country_id=entry.country_id, resource_id=resource_id, quantity=after
                    )
                )
            session.add(
                ResourceOperation(
                    country_id=entry.country_id,
                    resource_id=resource_id,
                    source="verdict_rollback",
                    source_id=str(verdict.id),
                    delta=delta,
                    before_value=current,
                    after_value=after,
                )
            )
    for entry in changes:
        await session.delete(entry)
    for project in projects:
        await session.delete(project)
    for project in research_projects:
        await session.delete(project)
