from datetime import datetime
from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vpi_bot.game_engine import ChangeRejected
from vpi_bot.models import (
    Country,
    CountryResource,
    EconomicOperation,
    EconomicStats,
    IndustryFacility,
    ResourceDefinition,
    ResourceOperation,
    now_utc,
)


def _amounts(values: dict, field: str) -> dict[str, Decimal]:
    if not isinstance(values, dict) or len(values) > 40:
        raise ChangeRejected(f"Некорректный список ресурсов в {field}")
    result = {}
    for name, raw in values.items():
        try:
            amount = Decimal(str(raw))
        except (InvalidOperation, ValueError) as exc:
            raise ChangeRejected(f"Некорректное количество ресурса «{name}»") from exc
        if (
            not name
            or len(name) > 100
            or not amount.is_finite()
            or amount <= 0
            or amount > Decimal(1000000)
        ):
            raise ChangeRejected(f"Недопустимое количество ресурса «{name}»")
        result[name] = amount
    return result


async def run_facility_cycle(
    session: AsyncSession, facility: IndustryFacility, *, now: datetime | None = None
) -> dict | None:
    """Apply one due production cycle in the caller's transaction; None means not due/unfunded."""
    now = now or now_utc()
    facility = await session.scalar(
        select(IndustryFacility).where(IndustryFacility.id == facility.id).with_for_update()
    )
    if not facility or facility.status != "active":
        return None
    previous = facility.last_tick_at
    if previous is not None:
        if previous.tzinfo is None and now.tzinfo is not None:
            previous = previous.replace(tzinfo=now.tzinfo)
        if (now - previous).total_seconds() < max(facility.interval_hours, 1) * 3600:
            return None
    inputs = _amounts(facility.input_per_tick, "потребления")
    outputs = _amounts(facility.production_per_tick, "производства")
    if not outputs:
        raise ChangeRejected("У предприятия не задан выпуск")
    country = await session.scalar(
        select(Country).where(Country.id == facility.country_id).with_for_update()
    )
    stats = await session.scalar(
        select(EconomicStats)
        .where(EconomicStats.country_id == facility.country_id)
        .with_for_update()
    )
    if not country or not country.is_active:
        return None
    if not stats:
        raise ChangeRejected("Предприятие не связано с экономической статистикой")
    cost = Decimal(facility.operating_cost)
    if not cost.is_finite() or cost < 0:
        raise ChangeRejected("Эксплуатационные расходы должны быть неотрицательны")
    if stats.treasury < cost:
        return None
    all_names = sorted(set(inputs) | set(outputs))
    definitions = (
        await session.scalars(
            select(ResourceDefinition)
            .where(
                ResourceDefinition.guild_id == country.guild_id,
                ResourceDefinition.name.in_(all_names),
                ResourceDefinition.enabled.is_(True),
            )
            .order_by(ResourceDefinition.id)
            .with_for_update()
        )
    ).all()
    by_name = {row.name: row for row in definitions}
    if set(by_name) != set(all_names):
        raise ChangeRejected("В рецепте предприятия есть незарегистрированный ресурс")
    rows = (
        await session.scalars(
            select(CountryResource)
            .where(
                CountryResource.country_id == country.id,
                CountryResource.resource_id.in_([row.id for row in definitions]),
            )
            .order_by(CountryResource.resource_id)
            .with_for_update()
        )
    ).all()
    inventory = {row.resource_id: row for row in rows}
    for name, amount in inputs.items():
        row = inventory.get(by_name[name].id)
        if row is None or row.quantity < amount:
            return None
    for name in all_names:
        definition = by_name[name]
        row = inventory.get(definition.id)
        if row is None:
            row = CountryResource(
                country_id=country.id, resource_id=definition.id, quantity=Decimal(0)
            )
            session.add(row)
            await session.flush()
            inventory[definition.id] = row
    max_inventory = Decimal("99999999999999999.999")
    for name, amount in outputs.items():
        if Decimal(inventory[by_name[name].id].quantity) + amount > max_inventory:
            raise ChangeRejected(f"Склад переполнен ресурсом «{name}»")
    changes = []
    for name, amount in inputs.items():
        row = inventory[by_name[name].id]
        before = Decimal(row.quantity)
        row.quantity = before - amount
        changes.append((by_name[name], -amount, before, row.quantity, "industry_input"))
    for name, amount in outputs.items():
        row = inventory[by_name[name].id]
        before = Decimal(row.quantity)
        row.quantity = before + amount
        changes.append((by_name[name], amount, before, row.quantity, "industry_output"))
    treasury_before = Decimal(stats.treasury)
    stats.treasury -= cost
    if cost:
        session.add(
            EconomicOperation(
                country_id=country.id,
                category="treasury",
                source="industry_cost",
                source_id=str(facility.id),
                delta=-cost,
                before_value=treasury_before,
                after_value=stats.treasury,
                created_at=now,
            )
        )
    facility.last_tick_at = now
    session.add_all(
        [
            ResourceOperation(
                country_id=country.id,
                resource_id=definition.id,
                source=source,
                source_id=str(facility.id),
                delta=delta,
                before_value=before,
                after_value=after,
                created_at=now,
            )
            for definition, delta, before, after, source in changes
        ]
    )
    return {
        "facility": facility.name,
        "inputs": {key: str(val) for key, val in inputs.items()},
        "outputs": {key: str(val) for key, val in outputs.items()},
        "cost": str(cost),
    }
