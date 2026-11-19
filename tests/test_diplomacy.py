from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from vpi_bot.diplomacy import parse_resource_amounts, settle_trade
from vpi_bot.game_engine import ChangeRejected
from vpi_bot.models import (
    Base,
    Country,
    CountryResource,
    CurrencyExchangeRate,
    EconomicOperation,
    EconomicStats,
    ResourceDefinition,
    ResourceOperation,
    TradeOffer,
)


def test_parse_resource_amounts():
    assert parse_resource_amounts("Железо=2.5; Древесина=4") == {"Железо": 2.5, "Древесина": 4.0}
    with pytest.raises(ValueError):
        parse_resource_amounts("железо=0")
    with pytest.raises(ValueError):
        parse_resource_amounts("неверный ввод")


@pytest.mark.asyncio
async def test_trade_is_atomic_and_requires_confirmation(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'trade.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        sender = Country(
            guild_id=10,
            owner_id=1,
            name="А",
            government="Республика",
            religion="Нет",
            currency="рубль",
        )
        receiver = Country(
            guild_id=10,
            owner_id=2,
            name="Б",
            government="Монархия",
            religion="Нет",
            currency="рубль",
        )
        sender.economy = EconomicStats(treasury=Decimal(100))
        receiver.economy = EconomicStats(treasury=Decimal(5))
        iron = ResourceDefinition(guild_id=10, name="Железо")
        session.add_all([sender, receiver, iron])
        await session.flush()
        session.add(CountryResource(country_id=sender.id, resource_id=iron.id, quantity=Decimal(8)))
        offer = TradeOffer(
            guild_id=10,
            from_country_id=sender.id,
            to_country_id=receiver.id,
            money=Decimal(20),
            resources={"Железо": 3},
            status="pending",
        )
        session.add(offer)
        await session.flush()
        await settle_trade(session, offer, 10)
        await session.commit()
        assert offer.status == "accepted"
        assert (await session.get(EconomicStats, sender.id)).treasury == Decimal(80)
        assert (await session.get(EconomicStats, receiver.id)).treasury == Decimal(25)
        assert (await session.get(CountryResource, (sender.id, iron.id))).quantity == Decimal(5)
        assert (await session.get(CountryResource, (receiver.id, iron.id))).quantity == Decimal(3)
        operations = (await session.scalars(select(ResourceOperation))).all()
        assert len(operations) == 2
        with pytest.raises(ChangeRejected, match="уже обработано"):
            await settle_trade(session, offer, 10)
    await engine.dispose()


@pytest.mark.asyncio
async def test_cross_currency_trade_uses_configured_directional_rate(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'exchange.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        sender = Country(
            guild_id=11,
            owner_id=1,
            name="С",
            government="Республика",
            religion="Нет",
            currency="Рубль",
        )
        receiver = Country(
            guild_id=11,
            owner_id=2,
            name="Т",
            government="Республика",
            religion="Нет",
            currency="доллар",
        )
        sender.economy = EconomicStats(treasury=Decimal(50))
        receiver.economy = EconomicStats(treasury=Decimal(3))
        session.add_all([sender, receiver])
        await session.flush()
        rate = CurrencyExchangeRate(
            guild_id=11,
            from_currency="рубль",
            to_currency="доллар",
            rate=Decimal("2.000000"),
            updated_by=7,
        )
        offer = TradeOffer(
            guild_id=11,
            from_country_id=sender.id,
            to_country_id=receiver.id,
            money=Decimal(10),
            status="pending",
        )
        session.add_all([rate, offer])
        await session.flush()
        await settle_trade(session, offer, 11)
        await session.commit()
        assert sender.economy.treasury == Decimal(40)
        assert receiver.economy.treasury == Decimal(23)
        assert offer.money_received == Decimal(20)
        assert offer.exchange_rate == Decimal("2.000000")
        operations = (await session.scalars(select(EconomicOperation))).all()
        assert {row.delta for row in operations} == {Decimal(-10), Decimal(20)}
    await engine.dispose()


@pytest.mark.asyncio
async def test_cross_currency_trade_without_rate_is_rejected(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'no-rate.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        sender = Country(
            guild_id=12,
            owner_id=1,
            name="С",
            government="Республика",
            religion="Нет",
            currency="рубль",
        )
        receiver = Country(
            guild_id=12,
            owner_id=2,
            name="Т",
            government="Республика",
            religion="Нет",
            currency="доллар",
        )
        sender.economy = EconomicStats(treasury=Decimal(50))
        receiver.economy = EconomicStats(treasury=Decimal(3))
        session.add_all([sender, receiver])
        await session.flush()
        offer = TradeOffer(
            guild_id=12,
            from_country_id=sender.id,
            to_country_id=receiver.id,
            money=Decimal(10),
            status="pending",
        )
        session.add(offer)
        await session.flush()
        with pytest.raises(ChangeRejected, match="не настроен"):
            await settle_trade(session, offer, 12)
        await session.rollback()
    await engine.dispose()
