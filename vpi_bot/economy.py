from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vpi_bot.models import Country, DailyRun, EconomicOperation, EconomicStats, now_utc


def _clamp(value: Decimal, low: Decimal, high: Decimal) -> Decimal:
    return min(max(value, low), high)


async def calculate_daily_economy(
    session: AsyncSession, country: Country, local_date: date
) -> dict | None:
    """Settle one country's daily budget once; None means another worker already settled it."""
    existing = await session.scalar(
        select(DailyRun).where(
            DailyRun.country_id == country.id,
            DailyRun.local_date == local_date,
            DailyRun.process == "economy",
        )
    )
    if existing:
        return None
    stats = await session.scalar(
        select(EconomicStats).where(EconomicStats.country_id == country.id).with_for_update()
    )
    if stats is None:
        raise ValueError("У государства нет экономики")
    # Recheck after locking the shared row: a concurrent worker may have committed the run.
    existing = await session.scalar(
        select(DailyRun).where(
            DailyRun.country_id == country.id,
            DailyRun.local_date == local_date,
            DailyRun.process == "economy",
        )
    )
    if existing:
        return None
    treasury_before, debt_before, gdp_before = stats.treasury, stats.debt, stats.gdp
    net = stats.income - stats.expenses
    if net >= 0:
        stats.treasury += net
        deficit = Decimal(0)
    else:
        covered = min(stats.treasury, -net)
        stats.treasury -= covered
        deficit = -net - covered
        stats.debt += deficit
    stats.gdp = max(Decimal(0), stats.gdp * (Decimal(1) + stats.growth / Decimal(100)))
    deficit_ratio = (deficit / max(stats.gdp, Decimal(1))) * Decimal(100)
    stats.inflation = _clamp(stats.inflation + deficit_ratio, Decimal(0), Decimal(1000))
    stats.unemployment = _clamp(
        stats.unemployment - stats.growth * Decimal("0.05"), Decimal(0), Decimal(100)
    )
    stability_delta = Decimal("0.2") if net >= 0 else -min(Decimal(5), deficit_ratio)
    stats.stability = _clamp(stats.stability + stability_delta, Decimal(0), Decimal(100))
    report = {
        "date": local_date.isoformat(),
        "treasury_before": str(treasury_before),
        "treasury_after": str(stats.treasury),
        "income": str(stats.income),
        "expenses": str(stats.expenses),
        "debt_before": str(debt_before),
        "debt_after": str(stats.debt),
        "gdp_before": str(gdp_before),
        "gdp_after": str(stats.gdp),
        "inflation": str(stats.inflation),
        "unemployment": str(stats.unemployment),
        "stability": str(stats.stability),
        "deficit": str(deficit),
    }
    if stats.treasury != treasury_before:
        session.add(
            EconomicOperation(
                country_id=country.id,
                category="treasury",
                source="daily_settlement",
                source_id=local_date.isoformat(),
                delta=stats.treasury - treasury_before,
                before_value=treasury_before,
                after_value=stats.treasury,
            )
        )
    if stats.debt != debt_before:
        session.add(
            EconomicOperation(
                country_id=country.id,
                category="debt",
                source="daily_settlement",
                source_id=local_date.isoformat(),
                delta=stats.debt - debt_before,
                before_value=debt_before,
                after_value=stats.debt,
            )
        )
    session.add(
        DailyRun(
            guild_id=country.guild_id,
            country_id=country.id,
            local_date=local_date,
            process="economy",
            report=report,
            completed_at=now_utc(),
        )
    )
    return report
