import logging
from decimal import Decimal

import discord
from discord import app_commands
from sqlalchemy import select

from vpi_bot.commands.autocomplete import country_choices, incoming_offer_choices
from vpi_bot.game_engine import ChangeRejected
from vpi_bot.models import Country
from vpi_bot.security import admin_check

log = logging.getLogger(__name__)


def register(bot) -> None:
    @bot.tree.command(name="exchange-rate-set", description="Настроить курс для денежных сделок")
    @app_commands.describe(
        from_currency="Исходная валюта, указанная в карточке страны",
        to_currency="Целевая валюта, в которую выполняется обмен",
        rate="Сколько единиц целевой валюты даётся за 1 единицу исходной",
        reason="Причина изменения курса для аудита",
    )
    @admin_check(bot)
    async def exchange_rate_set(
        interaction: discord.Interaction,
        from_currency: str,
        to_currency: str,
        rate: app_commands.Range[float, 0.000001, 1000000.0],
        reason: str,
    ):
        from vpi_bot.models import AuditLog, CurrencyExchangeRate

        source, target = from_currency.strip().casefold(), to_currency.strip().casefold()
        if not source or not target or len(source) > 80 or len(target) > 80 or source == target:
            await interaction.response.send_message("Укажите две разные валюты.", ephemeral=True)
            return
        async with bot.db() as session:
            row = await session.scalar(
                select(CurrencyExchangeRate)
                .where(
                    CurrencyExchangeRate.guild_id == interaction.guild_id,
                    CurrencyExchangeRate.from_currency == source,
                    CurrencyExchangeRate.to_currency == target,
                )
                .with_for_update()
            )
            old = str(row.rate) if row else None
            if row is None:
                row = CurrencyExchangeRate(
                    guild_id=interaction.guild_id,
                    from_currency=source,
                    to_currency=target,
                    rate=Decimal(str(rate)).quantize(Decimal("0.000001")),
                    updated_by=interaction.user.id,
                )
                session.add(row)
            else:
                row.rate = Decimal(str(rate)).quantize(Decimal("0.000001"))
                row.updated_by = interaction.user.id
            await session.flush()
            session.add(
                AuditLog(
                    guild_id=interaction.guild_id,
                    actor_id=interaction.user.id,
                    action="exchange_rate_set",
                    target_type="exchange_rate",
                    target_id=str(row.id),
                    old_value=old,
                    new_value=str(row.rate),
                    reason=reason[:1000],
                )
            )
            await session.commit()
        await interaction.response.send_message(
            f"Курс настроен: 1 {source} = {rate:g} {target}.", ephemeral=True
        )

    @bot.tree.command(name="exchange-rate-delete", description="Удалить направленный валютный курс")
    @app_commands.describe(
        from_currency="Исходная валюта удаляемого курса",
        to_currency="Целевая валюта удаляемого курса",
        reason="Причина удаления для аудита",
    )
    @admin_check(bot)
    async def exchange_rate_delete(
        interaction: discord.Interaction, from_currency: str, to_currency: str, reason: str
    ):
        from vpi_bot.models import AuditLog, CurrencyExchangeRate

        source, target = from_currency.strip().casefold(), to_currency.strip().casefold()
        async with bot.db() as session:
            row = await session.scalar(
                select(CurrencyExchangeRate)
                .where(
                    CurrencyExchangeRate.guild_id == interaction.guild_id,
                    CurrencyExchangeRate.from_currency == source,
                    CurrencyExchangeRate.to_currency == target,
                )
                .with_for_update()
            )
            if row is None:
                await interaction.response.send_message("Курс не найден.", ephemeral=True)
                return
            session.add(
                AuditLog(
                    guild_id=interaction.guild_id,
                    actor_id=interaction.user.id,
                    action="exchange_rate_delete",
                    target_type="exchange_rate",
                    target_id=str(row.id),
                    old_value=str(row.rate),
                    new_value=None,
                    reason=reason[:1000],
                )
            )
            await session.delete(row)
            await session.commit()
        await interaction.response.send_message("Курс удалён.", ephemeral=True)

    @bot.tree.command(
        name="trade-offer", description="Предложить перевод средств и ресурсов другому государству"
    )
    @app_commands.describe(
        recipient_country_id="ID государства-получателя из /countries",
        sender_country_id="ID вашего государства; укажите, если владеете несколькими",
        money="Сумма в валюте отправителя; 0, если передаются только ресурсы",
        resources='Ресурсы в формате "Железо=5; Древесина=2"; можно оставить пустым',
    )
    async def trade_offer(
        interaction: discord.Interaction,
        recipient_country_id: int,
        sender_country_id: int | None = None,
        money: app_commands.Range[float, 0.0, 1000000000000.0] = 0.0,
        resources: str = "",
    ):
        from vpi_bot.diplomacy import parse_resource_amounts
        from vpi_bot.models import TradeOffer

        if not interaction.guild_id or not await bot.guild_config(interaction.guild_id):
            await interaction.response.send_message("Сервер не настроен.", ephemeral=True)
            return
        try:
            resource_map = parse_resource_amounts(resources)
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        async with bot.db() as session:
            senders = (
                await session.scalars(
                    select(Country)
                    .where(
                        Country.guild_id == interaction.guild_id,
                        Country.owner_id == interaction.user.id,
                        Country.is_active.is_(True),
                    )
                    .order_by(Country.id)
                )
            ).all()
            sender = (
                next((row for row in senders if row.id == sender_country_id), None)
                if sender_country_id
                else None
            )
            if sender is None and sender_country_id is None and len(senders) == 1:
                sender = senders[0]
            if sender is None and len(senders) > 1:
                await interaction.response.send_message(
                    "Укажите `sender_country_id`, чтобы выбрать государство-отправителя.",
                    ephemeral=True,
                )
                return
            receiver = await session.scalar(
                select(Country).where(
                    Country.guild_id == interaction.guild_id,
                    Country.id == recipient_country_id,
                    Country.is_active.is_(True),
                )
            )
            if not sender or not receiver or sender.id == receiver.id:
                await interaction.response.send_message(
                    "Ваше государство или государство-получатель не найдено.", ephemeral=True
                )
                return
            money_amount = Decimal(str(money)).quantize(Decimal("0.01"))
            if money_amount <= 0 and not resource_map:
                await interaction.response.send_message(
                    "Укажите положительную сумму денег (минимум 0,01) или ресурсы для передачи.",
                    ephemeral=True,
                )
                return
            offer = TradeOffer(
                guild_id=interaction.guild_id,
                from_country_id=sender.id,
                to_country_id=receiver.id,
                money=money_amount,
                resources=resource_map,
            )
            session.add(offer)
            await session.commit()
            offer_id, channel_id, receiver_name = offer.id, receiver.channel_id, receiver.name
            sender_name, sender_currency, receiver_currency = (
                sender.name,
                sender.currency,
                receiver.currency,
            )
        await interaction.response.send_message(
            f"Предложение сделки #{offer_id} отправлено государству **{receiver_name}**. "
            "Передача произойдёт только после подтверждения `/trade-accept`.",
            ephemeral=True,
        )
        if channel_id and (channel := bot.get_channel(channel_id)):
            details = [f"Деньги: {money_amount:,.2f} {sender_currency}"] if money_amount else []
            if money_amount and sender_currency.casefold() != receiver_currency.casefold():
                details.append(
                    "Сумма будет пересчитана по заданному направленному курсу при принятии."
                )
            details += [f"{name}: {amount:g}" for name, amount in resource_map.items()]
            try:
                await channel.send(
                    f"🤝 **Предложение сделки #{offer_id}** от **{sender_name}** к "
                    f"**{receiver_name}**.\n"
                    + "\n".join(details)
                    + "\nДля принятия: `/trade-accept offer_id`."
                )
            except discord.HTTPException:
                log.exception("Failed to notify trade recipient")

    @trade_offer.autocomplete("recipient_country_id")
    async def trade_recipient_autocomplete(interaction: discord.Interaction, current: str):
        return await country_choices(interaction, current)

    @trade_offer.autocomplete("sender_country_id")
    async def trade_sender_autocomplete(interaction: discord.Interaction, current: str):
        return await country_choices(interaction, current, owner_only=True)

    @bot.tree.command(name="trade-accept", description="Подтвердить входящее торговое предложение")
    @app_commands.describe(offer_id="Номер входящего предложения из уведомления о сделке")
    async def trade_accept(interaction: discord.Interaction, offer_id: int):
        from vpi_bot.diplomacy import settle_trade
        from vpi_bot.models import TradeOffer

        async with bot.db() as session:
            offer = await session.scalar(
                select(TradeOffer)
                .where(TradeOffer.id == offer_id, TradeOffer.guild_id == interaction.guild_id)
                .with_for_update()
            )
            receiver = await session.get(Country, offer.to_country_id) if offer else None
            if (
                not offer
                or not receiver
                or receiver.owner_id != interaction.user.id
                or offer.status != "pending"
            ):
                await interaction.response.send_message(
                    "Входящее предложение не найдено или уже обработано.", ephemeral=True
                )
                return
            try:
                await settle_trade(session, offer, interaction.guild_id)
                sender = await session.get(Country, offer.from_country_id)
                received_amount, receiver_currency = offer.money_received, receiver.currency
                exchange_rate = offer.exchange_rate
                await session.commit()
            except ChangeRejected as exc:
                await session.rollback()
                await interaction.response.send_message(
                    f"Сделку невозможно исполнить: {exc}", ephemeral=True
                )
                return
        money_note = (
            f" Получено {received_amount:,.2f} {receiver_currency}"
            + (f" по курсу {exchange_rate}." if exchange_rate else ".")
            if received_amount
            else ""
        )
        await interaction.response.send_message(
            f"Сделка #{offer_id} исполнена транзакционно.{money_note}", ephemeral=True
        )
        if sender and sender.channel_id and (channel := bot.get_channel(sender.channel_id)):
            try:
                await channel.send(
                    f"✅ Государство **{receiver.name}** подтвердило сделку #{offer_id}. "
                    + (
                        f"Получено: {received_amount:,.2f} {receiver_currency}."
                        if received_amount
                        else ""
                    )
                )
            except discord.HTTPException:
                log.exception("Failed to notify trade sender")

    @trade_accept.autocomplete("offer_id")
    async def accept_offer_autocomplete(interaction: discord.Interaction, current: str):
        return await incoming_offer_choices(interaction, current)

    @bot.tree.command(name="trade-decline", description="Отклонить входящее торговое предложение")
    @app_commands.describe(offer_id="Номер входящего предложения из уведомления о сделке")
    async def trade_decline(interaction: discord.Interaction, offer_id: int):
        from vpi_bot.models import TradeOffer

        async with bot.db() as session:
            offer = await session.scalar(
                select(TradeOffer)
                .where(TradeOffer.id == offer_id, TradeOffer.guild_id == interaction.guild_id)
                .with_for_update()
            )
            receiver = await session.get(Country, offer.to_country_id) if offer else None
            if (
                not offer
                or not receiver
                or receiver.owner_id != interaction.user.id
                or offer.status != "pending"
            ):
                await interaction.response.send_message(
                    "Входящее предложение не найдено или уже обработано.", ephemeral=True
                )
                return
            offer.status, offer.resolved_at = "rejected", discord.utils.utcnow()
            await session.commit()
        await interaction.response.send_message(
            f"Предложение #{offer_id} отклонено.", ephemeral=True
        )

    @trade_decline.autocomplete("offer_id")
    async def decline_offer_autocomplete(interaction: discord.Interaction, current: str):
        return await incoming_offer_choices(interaction, current)
