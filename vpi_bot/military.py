from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vpi_bot.game_engine import ChangeRejected
from vpi_bot.models import Country, EconomicStats, MilitaryStats

METRICS = {"personnel", "budget", "technology_level", "readiness", "supply"}
SECTIONS = {"units", "equipment", "ammunition"}


async def adjust_military(
    session: AsyncSession,
    *,
    guild_id: int,
    country_id: int,
    section: str,
    key: str,
    raw_delta: float | Decimal,
) -> tuple[Country, str, str, Decimal, Decimal | None, Decimal | None]:
    """Apply an audited-admin military delta; budget changes transfer to/from treasury."""
    country = await session.scalar(
        select(Country)
        .where(Country.id == country_id, Country.guild_id == guild_id, Country.is_active.is_(True))
        .with_for_update()
    )
    if not country:
        raise ChangeRejected("Действующее государство не найдено")
    try:
        delta = Decimal(str(raw_delta))
    except (InvalidOperation, ValueError) as exc:
        raise ChangeRejected("Изменение должно быть числом") from exc
    if not delta.is_finite() or delta == 0:
        raise ChangeRejected("Укажите конечное ненулевое изменение")
    key = key.strip()
    if section == "metric":
        key = key.casefold()

    army = await session.scalar(
        select(MilitaryStats).where(MilitaryStats.country_id == country_id).with_for_update()
    )
    if army is None:
        army = MilitaryStats(country_id=country_id)
        session.add(army)
        await session.flush()

    treasury_delta = Decimal(0)
    treasury_before = treasury_after = None
    if section == "metric":
        if key not in METRICS:
            raise ChangeRejected("Неизвестный военный показатель")
        if key in {"budget", "readiness", "supply"}:
            delta = delta.quantize(Decimal("0.01"))
            if delta == 0:
                raise ChangeRejected("Минимальное изменение показателя — 0,01")
        if key in {"personnel", "technology_level"} and delta != delta.to_integral_value():
            raise ChangeRejected("Личный состав и уровень техники изменяются целым числом")
        before = Decimal(str(getattr(army, key)))
        after = before + delta
        bounds = {
            "personnel": (Decimal(0), Decimal(100000000)),
            "technology_level": (Decimal(0), Decimal(1000)),
            "readiness": (Decimal(0), Decimal(100)),
            "supply": (Decimal(0), Decimal(100)),
            "budget": (Decimal(0), Decimal("999999999999999999.99")),
        }
        low, high = bounds[key]
        if not low <= after <= high:
            raise ChangeRejected("Изменение выходит за допустимый диапазон")
        if key == "budget":
            stats = await session.scalar(
                select(EconomicStats)
                .where(EconomicStats.country_id == country_id)
                .with_for_update()
            )
            if not stats:
                raise ChangeRejected("Экономическая статистика не найдена")
            treasury_delta = -delta
            treasury_before = Decimal(stats.treasury)
            treasury_after = treasury_before + treasury_delta
            if treasury_after < 0 or treasury_after > Decimal("999999999999999999.99"):
                raise ChangeRejected("Недостаточно казны для такого изменения военного бюджета")
            stats.treasury = treasury_after
        setattr(army, key, int(after) if key in {"personnel", "technology_level"} else after)
    elif section in SECTIONS:
        if not key or len(key) > 100:
            raise ChangeRejected(
                "Название подразделения или имущества должно содержать 1–100 символов"
            )
        values = dict(getattr(army, section) or {})
        before = Decimal(str(values.get(key, 0)))
        after = before + delta
        if after < 0 or after > Decimal(1000000000):
            raise ChangeRejected("Количество не может быть отрицательным или превышать лимит")
        values[key] = float(after)
        setattr(army, section, values)
    else:
        raise ChangeRejected("Выберите показатель, подразделения, технику или боеприпасы")

    return country, str(before), str(after), treasury_delta, treasury_before, treasury_after
