from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vpi_bot.game_engine import ChangeRejected
from vpi_bot.models import (
    Country,
    CountryResource,
    ResourceDefinition,
    ResourceOperation,
    ResourceProduction,
    now_utc,
)


async def run_resource_production_cycle(
    session: AsyncSession, production_id: int, *, now: datetime | None = None
) -> dict | None:
    """Apply a due resource tick once in the caller's transaction."""
    now = now or now_utc()
    candidate = await session.scalar(
        select(ResourceProduction).where(ResourceProduction.id == production_id)
    )
    if candidate is None:
        return None
    # Country first serializes this country's production rows and inventory creation.
    country = await session.scalar(
        select(Country).where(Country.id == candidate.country_id).with_for_update()
    )
    if country is None or not country.is_active:
        return None
    production = await session.scalar(
        select(ResourceProduction)
        .where(
            ResourceProduction.id == production_id,
            ResourceProduction.enabled.is_(True),
        )
        .with_for_update()
    )
    if production is None or production.country_id != country.id:
        return None
    previous = production.last_tick_at
    if previous is not None:
        if previous.tzinfo is None and now.tzinfo is not None:
            previous = previous.replace(tzinfo=now.tzinfo)
        if (now - previous).total_seconds() < max(production.interval_hours, 1) * 3600:
            return None
    resource = await session.scalar(
        select(ResourceDefinition)
        .where(
            ResourceDefinition.id == production.resource_id,
            ResourceDefinition.guild_id == country.guild_id,
            ResourceDefinition.enabled.is_(True),
        )
        .with_for_update()
    )
    if resource is None:
        return None
    amount = Decimal(production.amount_per_tick)
    if not amount.is_finite() or amount <= 0:
        raise ChangeRejected("План добычи должен содержать положительное количество ресурса")
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
    before = Decimal(inventory.quantity)
    after = before + amount
    if after > Decimal("99999999999999999.999"):
        raise ChangeRejected(f"Склад переполнен ресурсом «{resource.name}»")
    inventory.quantity = after
    production.last_tick_at = now
    session.add(
        ResourceOperation(
            country_id=country.id,
            resource_id=resource.id,
            source="production",
            source_id=str(production.id),
            delta=amount,
            before_value=before,
            after_value=after,
            created_at=now,
        )
    )
    return {
        "country_id": country.id,
        "country_name": country.name,
        "resource_name": resource.name,
        "amount": str(amount),
        "before": str(before),
        "after": str(after),
    }
