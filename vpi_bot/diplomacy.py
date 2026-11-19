from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vpi_bot.game_engine import ChangeRejected
from vpi_bot.models import (
    Country,
    CountryResource,
    CurrencyExchangeRate,
    EconomicOperation,
    EconomicStats,
    ResourceDefinition,
    ResourceOperation,
    TradeOffer,
    now_utc,
)


def parse_resource_amounts(value: str) -> dict[str, float]:
    """Parse the compact admin/player input format 'Железо=5; Древесина=10'."""
    result: dict[str, float] = {}
    if not value.strip():
        return result
    for item in value.split(";"):
        if "=" not in item:
            raise ValueError("Формат ресурсов: Название=количество; Название2=количество")
        name, amount_text = item.rsplit("=", 1)
        name = name.strip()
        try:
            amount = Decimal(amount_text.strip())
        except InvalidOperation as exc:
            raise ValueError(f"Некорректное количество ресурса «{name}»") from exc
        if not name or not amount.is_finite() or amount <= 0 or amount > Decimal(1000000000000):
            raise ValueError("Количество каждого передаваемого ресурса должно быть положительным")
        amount = amount.quantize(Decimal("0.001"))
        if amount <= 0:
            raise ValueError("Минимальный перевод ресурса — 0,001 единицы")
        result[name] = float(amount)
    return result


async def settle_trade(session: AsyncSession, offer: TradeOffer, guild_id: int) -> None:
    """Transfer escrow-free offer contents in one DB transaction; call commit after success."""
    locked = await session.scalar(
        select(TradeOffer).where(TradeOffer.id == offer.id).with_for_update()
    )
    if not locked or locked.status != "pending" or locked.guild_id != guild_id:
        raise ChangeRejected("Предложение уже обработано или не принадлежит этому серверу")
    countries = (
        await session.scalars(
            select(Country)
            .where(
                Country.id.in_([locked.from_country_id, locked.to_country_id]),
                Country.guild_id == guild_id,
                Country.is_active.is_(True),
            )
            .order_by(Country.id)
            .with_for_update()
        )
    ).all()
    country_by_id = {row.id: row for row in countries}
    sender = country_by_id.get(locked.from_country_id)
    receiver = country_by_id.get(locked.to_country_id)
    if not sender or not receiver or sender.id == receiver.id:
        raise ChangeRejected("Государства сделки некорректны")
    money = Decimal(locked.money).quantize(Decimal("0.01"))
    exchange_rate = Decimal(1)
    if money > 0 and sender.currency.casefold() != receiver.currency.casefold():
        rate_row = await session.scalar(
            select(CurrencyExchangeRate)
            .where(
                CurrencyExchangeRate.guild_id == guild_id,
                CurrencyExchangeRate.from_currency == sender.currency.casefold(),
                CurrencyExchangeRate.to_currency == receiver.currency.casefold(),
            )
            .with_for_update()
        )
        if rate_row is None:
            raise ChangeRejected("Для этих валют не настроен направленный курс обмена")
        exchange_rate = Decimal(rate_row.rate)
        if not exchange_rate.is_finite() or exchange_rate <= 0:
            raise ChangeRejected("Курс обмена некорректен")
    money_received = (money * exchange_rate).quantize(Decimal("0.01"))
    stat_rows = (
        await session.scalars(
            select(EconomicStats)
            .where(EconomicStats.country_id.in_([sender.id, receiver.id]))
            .order_by(EconomicStats.country_id)
            .with_for_update()
        )
    ).all()
    stats_by_country = {row.country_id: row for row in stat_rows}
    sender_stats = stats_by_country.get(sender.id)
    receiver_stats = stats_by_country.get(receiver.id)
    if not sender_stats or not receiver_stats:
        raise ChangeRejected("У государства отсутствует экономическая статистика")
    if money < 0 or sender_stats.treasury < money:
        raise ChangeRejected("У отправителя недостаточно средств")
    sender_before, receiver_before = sender_stats.treasury, receiver_stats.treasury
    sender_stats.treasury -= money
    receiver_stats.treasury += money_received
    if receiver_stats.treasury >= Decimal(1000000000000000000):
        raise ChangeRejected("Перевод превышает вместимость казны получателя")
    locked.money_received = money_received
    locked.exchange_rate = (
        exchange_rate if sender.currency.casefold() != receiver.currency.casefold() else None
    )
    if money:
        session.add_all(
            [
                EconomicOperation(
                    country_id=sender.id,
                    category="treasury",
                    source="trade_out",
                    source_id=str(locked.id),
                    delta=-money,
                    before_value=sender_before,
                    after_value=sender_stats.treasury,
                ),
                EconomicOperation(
                    country_id=receiver.id,
                    category="treasury",
                    source="trade_in",
                    source_id=str(locked.id),
                    delta=money_received,
                    before_value=receiver_before,
                    after_value=receiver_stats.treasury,
                ),
            ]
        )
    for name, raw_amount in sorted(locked.resources.items()):
        definition = await session.scalar(
            select(ResourceDefinition).where(
                ResourceDefinition.guild_id == guild_id,
                ResourceDefinition.name == name,
                ResourceDefinition.enabled.is_(True),
            )
        )
        if not definition:
            raise ChangeRejected(f"Ресурс «{name}» больше не доступен")
        amount = Decimal(str(raw_amount))
        source = await session.scalar(
            select(CountryResource)
            .where(
                CountryResource.country_id == sender.id,
                CountryResource.resource_id == definition.id,
            )
            .with_for_update()
        )
        if amount <= 0 or not source or source.quantity < amount:
            raise ChangeRejected(f"У отправителя недостаточно ресурса «{name}»")
        target = await session.scalar(
            select(CountryResource)
            .where(
                CountryResource.country_id == receiver.id,
                CountryResource.resource_id == definition.id,
            )
            .with_for_update()
        )
        if target is None:
            target = CountryResource(
                country_id=receiver.id, resource_id=definition.id, quantity=Decimal(0)
            )
            session.add(target)
            await session.flush()
        source_before, target_before = source.quantity, target.quantity
        if target_before + amount > Decimal("99999999999999999.999"):
            raise ChangeRejected(f"Склад получателя переполнен ресурсом «{name}»")
        source.quantity -= amount
        target.quantity += amount
        session.add_all(
            [
                ResourceOperation(
                    country_id=sender.id,
                    resource_id=definition.id,
                    source="trade_out",
                    source_id=str(locked.id),
                    delta=-amount,
                    before_value=source_before,
                    after_value=source.quantity,
                ),
                ResourceOperation(
                    country_id=receiver.id,
                    resource_id=definition.id,
                    source="trade_in",
                    source_id=str(locked.id),
                    delta=amount,
                    before_value=target_before,
                    after_value=target.quantity,
                ),
            ]
        )
    locked.status = "accepted"
    locked.resolved_at = now_utc()
