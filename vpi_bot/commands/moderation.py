import json
import logging
from datetime import timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from sqlalchemy import select

from vpi_bot.ai.schemas import JudgeResult, ProposedChanges
from vpi_bot.commands.autocomplete import country_choices, failed_post_choices, verdict_choices
from vpi_bot.game_engine import ChangeRejected
from vpi_bot.models import Country, EconomicStats, GamePost, PostLimitGrant, Verdict
from vpi_bot.security import admin_check
from vpi_bot.ui.cards import verdict_embed

log = logging.getLogger(__name__)


async def _approve_pending_verdict(
    bot, interaction, verdict_id: int
) -> tuple[int | None, int | None]:
    async with bot.db() as session:
        verdict = await session.scalar(
            select(Verdict).where(Verdict.id == verdict_id).with_for_update()
        )
        post = await session.get(GamePost, verdict.post_id) if verdict else None
        country = (
            await session.scalar(
                select(Country).where(Country.id == post.country_id).with_for_update()
            )
            if post
            else None
        )
        if (
            not verdict
            or not post
            or not country
            or post.guild_id != interaction.guild_id
            or verdict.state != "pending"
        ):
            raise ValueError("Ожидающий вердикт не найден или уже обработан.")
        from vpi_bot.game_engine import apply_changes

        verdict.applied_changes = await apply_changes(
            session, country, verdict, verdict.proposed_changes
        )
        verdict.state = "applied"
        post.state = "judged"
        from vpi_bot.models import AuditLog, HistoricalEvent

        session.add(
            AuditLog(
                guild_id=interaction.guild_id,
                actor_id=interaction.user.id,
                action="verdict_approve",
                target_type="verdict",
                target_id=str(verdict.id),
                old_value="pending",
                new_value="applied",
                reason="Одобрено модератором",
            )
        )
        session.add(
            HistoricalEvent(
                verdict_id=verdict.id,
                country_id=country.id,
                title=verdict.title,
                content=verdict.description,
                importance=8 if verdict.status == "success" else 6,
                tags=[verdict.status],
            )
        )
        await session.flush()
        from vpi_bot.lore import refresh_event_summary

        await refresh_event_summary(session, country.id)
        channel_id, message_id = post.channel_id, verdict.message_id
        await session.commit()
    from vpi_bot.bot import update_verdict_card

    await update_verdict_card(bot, channel_id, message_id, "Одобрено модератором")
    return channel_id, message_id


async def _reject_pending_verdict(bot, interaction, verdict_id: int, reason: str) -> None:
    async with bot.db() as session:
        verdict = await session.scalar(
            select(Verdict).where(Verdict.id == verdict_id).with_for_update()
        )
        if not verdict:
            raise ValueError("Вердикт не найден.")
        post = await session.get(GamePost, verdict.post_id)
        if not post or post.guild_id != interaction.guild_id or verdict.state != "pending":
            raise ValueError("Вердикт не принадлежит серверу или уже обработан.")
        verdict.state, verdict.moderator_comment = "rejected", reason[:1000]
        from vpi_bot.models import AuditLog

        session.add(
            AuditLog(
                guild_id=interaction.guild_id,
                actor_id=interaction.user.id,
                action="verdict_reject",
                target_type="verdict",
                target_id=str(verdict.id),
                old_value="pending",
                new_value="rejected",
                reason=reason[:1000],
            )
        )
        channel_id, message_id = post.channel_id, verdict.message_id
        await session.commit()
    from vpi_bot.bot import update_verdict_card

    await update_verdict_card(bot, channel_id, message_id, "Отклонено модератором", reason)


async def _review_embed(bot, guild_id: int, verdict_id: int):
    async with bot.db() as session:
        verdict = await session.scalar(
            select(Verdict).where(Verdict.id == verdict_id, Verdict.state == "pending")
        )
        post = await session.get(GamePost, verdict.post_id) if verdict else None
        country = await session.get(Country, post.country_id) if post else None
    if not verdict or not post or not country or post.guild_id != guild_id:
        return None
    result = JudgeResult(
        is_game_action=True,
        status=verdict.status,
        title=verdict.title,
        description=verdict.description,
        reasoning=verdict.reasoning,
        positives=verdict.consequences.get("positives", []),
        negatives=verdict.consequences.get("negatives", []),
        conditions=verdict.consequences.get("conditions", []),
        changes=ProposedChanges.model_validate(verdict.proposed_changes),
        duration_turns=verdict.duration_turns,
    )
    from vpi_bot.ui.cards import verdict_embed

    embed = verdict_embed(
        result,
        country,
        post.discord_message_id,
        verdict.created_at.strftime("%d.%m.%Y %H:%M UTC"),
        verdict_id=verdict.id,
        posts_remaining=verdict.consequences.get("posts_remaining"),
    )
    embed.add_field(
        name="🕒 Состояние", value="Ожидает решения; изменения ещё не применены", inline=True
    )
    embed.add_field(name="👤 Игрок", value=f"<@{post.author_id}>", inline=True)
    embed.add_field(
        name="🔗 Исходный пост",
        value=f"[Открыть сообщение](https://discord.com/channels/{guild_id}/{post.channel_id}/{post.discord_message_id})",
        inline=True,
    )
    embed.add_field(name="Текст поста", value=post.body[:700] or "(пусто)", inline=False)
    return embed


class _VerdictReviewSelect(discord.ui.Select):
    def __init__(self, view, rows):
        options = [
            discord.SelectOption(
                label=f"#{verdict_id} · {title}"[:100],
                description=f"{country_name} · игрок {author_id}"[:100],
                value=str(verdict_id),
            )
            for verdict_id, title, country_name, author_id in rows
        ]
        super().__init__(placeholder="Выберите ожидающий вердикт", options=options, row=0)
        self.review_view = view

    async def callback(self, interaction: discord.Interaction) -> None:
        verdict_id = int(self.values[0])
        embed = await _review_embed(self.review_view.bot, self.review_view.guild_id, verdict_id)
        if embed is None:
            await interaction.response.send_message(
                "Вердикт уже обработан или не найден.", ephemeral=True
            )
            return
        self.review_view.selected_verdict_id = verdict_id
        for item in self.review_view.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = False
        await interaction.response.edit_message(embed=embed, view=self.review_view)


class _RejectVerdictModal(discord.ui.Modal, title="Причина отклонения"):
    reason = discord.ui.TextInput(
        label="Короткая причина",
        placeholder="Например: действие нарушает правила эпохи",
        max_length=500,
        style=discord.TextStyle.paragraph,
    )

    def __init__(self, bot, verdict_id: int, review_message):
        super().__init__(timeout=300)
        self.bot, self.verdict_id, self.review_message = bot, verdict_id, review_message

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            await _reject_pending_verdict(
                self.bot, interaction, self.verdict_id, self.reason.value.strip()
            )
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.send_message(
            "Вердикт отклонён; причина записана в аудит.", ephemeral=True
        )
        if self.review_message:
            try:
                await self.review_message.edit(
                    content="⛔ Вердикт отклонён.", embed=None, view=None
                )
            except discord.HTTPException:
                pass


class VerdictReviewView(discord.ui.View):
    def __init__(self, bot, guild_id: int, owner_id: int, rows):
        super().__init__(timeout=900)
        self.bot, self.guild_id, self.owner_id = bot, guild_id, owner_id
        self.selected_verdict_id = None
        self.add_item(_VerdictReviewSelect(self, rows))
        self.approve_button.disabled = True
        self.reject_button.disabled = True

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id or interaction.guild_id != self.guild_id:
            await interaction.response.send_message(
                "Эта очередь открыта другим модератором.", ephemeral=True
            )
            return False
        if interaction.permissions.administrator:
            return True
        config = await self.bot.guild_config(interaction.guild_id)
        allowed_roles = {config.admin_role_id, config.moderator_role_id} if config else set()
        if not any(role.id in allowed_roles for role in getattr(interaction.user, "roles", ())):
            await interaction.response.send_message(
                "Недостаточно прав для модерации.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="Одобрить", emoji="✅", style=discord.ButtonStyle.success, row=1)
    async def approve_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.selected_verdict_id is None:
            await interaction.response.send_message("Сначала выберите вердикт.", ephemeral=True)
            return
        try:
            await _approve_pending_verdict(self.bot, interaction, self.selected_verdict_id)
        except (ValueError, ChangeRejected) as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.edit_message(
            content="✅ Вердикт одобрен, изменения применены.", embed=None, view=None
        )
        self.stop()

    @discord.ui.button(label="Отклонить", emoji="⛔", style=discord.ButtonStyle.danger, row=1)
    async def reject_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.selected_verdict_id is None:
            await interaction.response.send_message("Сначала выберите вердикт.", ephemeral=True)
            return
        await interaction.response.send_modal(
            _RejectVerdictModal(self.bot, self.selected_verdict_id, interaction.message)
        )


def register(bot) -> None:
    @bot.tree.command(name="verdict-review", description="Открыть интерактивную очередь модерации")
    @admin_check(bot, allow_moderator=True)
    async def verdict_review(interaction: discord.Interaction):
        async with bot.db() as session:
            rows = (
                await session.execute(
                    select(Verdict.id, Verdict.title, Country.name, GamePost.author_id)
                    .join(GamePost, GamePost.id == Verdict.post_id)
                    .join(Country, Country.id == GamePost.country_id)
                    .where(GamePost.guild_id == interaction.guild_id, Verdict.state == "pending")
                    .order_by(Verdict.created_at)
                    .limit(25)
                )
            ).all()
        if not rows:
            await interaction.response.send_message("✅ Ожидающих вердиктов нет.", ephemeral=True)
            return
        embed = discord.Embed(
            title="⚖️ Очередь модерации",
            description=(
                f"Показаны до **{len(rows)}** самых старых ожидающих решений. Выберите карточку из списка, "
                "прочитайте исходный пост и нажмите «Одобрить» или «Отклонить»."
            ),
            color=0x5865F2,
        )
        await interaction.response.send_message(
            embed=embed,
            view=VerdictReviewView(bot, interaction.guild_id, interaction.user.id, rows),
            ephemeral=True,
        )

    @bot.tree.command(name="verdict-approve", description="Одобрить вердикт по его ID")
    @app_commands.describe(verdict_id="ID ожидающего вердикта из /verdict-review")
    @admin_check(bot, allow_moderator=True)
    async def verdict_approve(interaction: discord.Interaction, verdict_id: int):
        async with bot.db() as session:
            verdict = await session.get(Verdict, verdict_id)
            post = await session.get(GamePost, verdict.post_id) if verdict else None
            country = (
                await session.scalar(
                    select(Country).where(Country.id == post.country_id).with_for_update()
                )
                if post
                else None
            )
            if (
                not verdict
                or not post
                or post.guild_id != interaction.guild_id
                or verdict.state != "pending"
            ):
                await interaction.response.send_message(
                    "Ожидающий вердикт не найден.", ephemeral=True
                )
                return
            from vpi_bot.game_engine import apply_changes

            try:
                verdict.applied_changes = await apply_changes(
                    session, country, verdict, verdict.proposed_changes
                )
            except ChangeRejected as exc:
                await session.rollback()
                await interaction.response.send_message(
                    f"Изменения не применены: {exc}", ephemeral=True
                )
                return
            verdict.state = "applied"
            post.state = "judged"
            from vpi_bot.models import AuditLog, HistoricalEvent

            session.add(
                AuditLog(
                    guild_id=interaction.guild_id,
                    actor_id=interaction.user.id,
                    action="verdict_approve",
                    target_type="verdict",
                    target_id=str(verdict.id),
                    old_value="pending",
                    new_value="applied",
                    reason="Одобрено администратором",
                )
            )

            event = HistoricalEvent(
                verdict_id=verdict.id,
                country_id=country.id,
                title=verdict.title,
                content=verdict.description,
                importance=8 if verdict.status == "success" else 6,
                tags=[verdict.status],
            )
            session.add(event)
            await session.flush()
            from vpi_bot.lore import refresh_event_summary

            await refresh_event_summary(session, country.id)
            await session.commit()
            channel_id, message_id = post.channel_id, verdict.message_id
        from vpi_bot.bot import update_verdict_card

        await update_verdict_card(bot, channel_id, message_id, "Одобрено администратором")
        await interaction.response.send_message(
            "Вердикт одобрен, изменения применены.", ephemeral=True
        )

    @verdict_approve.autocomplete("verdict_id")
    @admin_check(bot, allow_moderator=True)
    async def approve_verdict_autocomplete(interaction: discord.Interaction, current: str):
        return await verdict_choices(interaction, current, states=("pending",))

    @bot.tree.command(name="verdict-reject", description="Отклонить вердикт по его ID")
    @app_commands.describe(
        verdict_id="ID ожидающего вердикта из /verdict-review",
        reason="Коротко укажите причину; она сохранится в журнале и карточке",
    )
    @admin_check(bot, allow_moderator=True)
    async def verdict_reject(interaction: discord.Interaction, verdict_id: int, reason: str):
        async with bot.db() as session:
            verdict = await session.get(Verdict, verdict_id)
            if not verdict:
                await interaction.response.send_message("Вердикт не найден.", ephemeral=True)
                return
            post = await session.get(GamePost, verdict.post_id)
            if not post or post.guild_id != interaction.guild_id or verdict.state != "pending":
                await interaction.response.send_message(
                    "Вердикт не принадлежит этому серверу или уже обработан.", ephemeral=True
                )
                return
            verdict.state, verdict.moderator_comment = "rejected", reason[:1000]
            from vpi_bot.models import AuditLog

            session.add(
                AuditLog(
                    guild_id=interaction.guild_id,
                    actor_id=interaction.user.id,
                    action="verdict_reject",
                    target_type="verdict",
                    target_id=str(verdict.id),
                    old_value="pending",
                    new_value="rejected",
                    reason=reason[:1000],
                )
            )
            channel_id, message_id = post.channel_id, verdict.message_id
            await session.commit()
        from vpi_bot.bot import update_verdict_card

        await update_verdict_card(bot, channel_id, message_id, "Отклонено администратором", reason)
        await interaction.response.send_message("Вердикт отклонён.", ephemeral=True)

    @verdict_reject.autocomplete("verdict_id")
    @admin_check(bot, allow_moderator=True)
    async def reject_verdict_autocomplete(interaction: discord.Interaction, current: str):
        return await verdict_choices(interaction, current, states=("pending",))

    @bot.tree.command(name="outbox-status", description="Показать очередь недоставленных вердиктов")
    @admin_check(bot)
    async def outbox_status(interaction: discord.Interaction):
        from vpi_bot.models import VerdictOutbox

        async with bot.db() as session:
            rows = (
                await session.execute(
                    select(VerdictOutbox, GamePost.discord_message_id)
                    .join(GamePost, GamePost.id == VerdictOutbox.post_id)
                    .where(
                        VerdictOutbox.guild_id == interaction.guild_id,
                        VerdictOutbox.status != "sent",
                    )
                    .order_by(VerdictOutbox.created_at)
                    .limit(20)
                )
            ).all()
        lines = [
            f"Пост `{post_id}` · {row.status} · попыток {row.attempts} · "
            + (
                "ожидает ручного повтора"
                if row.status == "dead"
                else f"следующая {row.next_attempt_at:%d.%m %H:%M UTC}"
            )
            + (f" · ошибка: {row.last_error[:120]}" if row.last_error else "")
            for row, post_id in rows
        ]
        await interaction.response.send_message(
            ("\n".join(lines)[:1900] or "Недоставленных ответов нет."),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @bot.tree.command(
        name="post-retry", description="Повторно обработать сообщение, завершившееся ошибкой"
    )
    @app_commands.describe(
        discord_message_id="Выберите ошибочный пост из списка или введите ID из ссылки на Discord-сообщение"
    )
    @admin_check(bot)
    async def post_retry(interaction: discord.Interaction, discord_message_id: str):
        try:
            message_id = int(discord_message_id)
        except ValueError:
            await interaction.response.send_message(
                "ID сообщения должен быть числом.", ephemeral=True
            )
            return
        outbox_retry_id = None
        async with bot.db() as session:
            from vpi_bot.models import AuditLog, VerdictOutbox
            from vpi_bot.outbox import request_retry

            post = await session.scalar(
                select(GamePost)
                .where(
                    GamePost.discord_message_id == message_id,
                    GamePost.guild_id == interaction.guild_id,
                )
                .with_for_update()
            )
            if not post:
                await interaction.response.send_message("Игровой пост не найден.", ephemeral=True)
                return
            old_state = post.state
            verdict = await session.scalar(select(Verdict).where(Verdict.post_id == post.id))
            republish = verdict is not None and verdict.message_id is None
            retryable = post.state == "failed" and verdict is None
            if post.state in {"received", "processing"} and verdict is None:
                now = discord.utils.utcnow()
                started = post.processing_started_at or post.created_at
                if started.tzinfo is None:
                    started = started.replace(tzinfo=now.tzinfo)
                retryable = now - started >= timedelta(minutes=10)
                if retryable:
                    post.state = "failed"
                    await session.commit()
            if retryable:
                republish = False
            elif republish:
                republish = True
                outbox = await session.scalar(
                    select(VerdictOutbox).where(VerdictOutbox.post_id == post.id).with_for_update()
                )
                if outbox and outbox.status == "sent" and outbox.delivered_message_id:
                    verdict.message_id = outbox.delivered_message_id
                    await session.commit()
                    await interaction.response.send_message(
                        "Ответ уже опубликован; ссылка на сообщение восстановлена.", ephemeral=True
                    )
                    return
                if outbox:
                    outbox_retry_id = await request_retry(session, post.id)
            else:
                await interaction.response.send_message(
                    "Для сообщения уже есть ответ или оно не требует повторной обработки.",
                    ephemeral=True,
                )
                return
            channel_id, country_id, post_body = post.channel_id, post.country_id, post.body
            verdict_data = (
                {
                    "id": verdict.id,
                    "status": verdict.status,
                    "title": verdict.title,
                    "description": verdict.description,
                    "reasoning": verdict.reasoning,
                    "consequences": verdict.consequences,
                    "changes": verdict.proposed_changes,
                    "duration": verdict.duration_turns,
                    "state": verdict.state,
                }
                if republish
                else None
            )
            session.add(
                AuditLog(
                    guild_id=interaction.guild_id,
                    actor_id=interaction.user.id,
                    action="verdict_republish" if republish else "post_retry",
                    target_type="game_post",
                    target_id=str(post.id),
                    old_value=old_state,
                    new_value="reply_republish" if republish else "retry_requested",
                )
            )
            await session.commit()
        channel = bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await bot.fetch_channel(channel_id)
            except discord.HTTPException:
                channel = None
        if channel is None:
            await interaction.response.send_message(
                "Исходный канал недоступен боту.", ephemeral=True
            )
            return
        try:
            message = await channel.fetch_message(message_id)
        except discord.HTTPException:
            await interaction.response.send_message(
                "Исходное сообщение не найдено или недоступно.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        if outbox_retry_id is not None:
            await bot.flush_outbox()
            await interaction.followup.send(
                "Повторная доставка ответа поставлена в очередь; бот продолжит попытки автоматически.",
                ephemeral=True,
            )
            return
        if not republish:
            await bot.process_game_post(message, post_body)
        else:
            country = None
            async with bot.db() as session:
                country = await session.get(Country, country_id)
            result = JudgeResult(
                is_game_action=True,
                status=verdict_data["status"],
                title=verdict_data["title"],
                description=verdict_data["description"],
                reasoning=verdict_data["reasoning"],
                positives=verdict_data["consequences"].get("positives", []),
                negatives=verdict_data["consequences"].get("negatives", []),
                conditions=verdict_data["consequences"].get("conditions", []),
                changes=ProposedChanges.model_validate(verdict_data["changes"]),
                duration_turns=verdict_data["duration"],
            )
            embed = verdict_embed(
                result,
                country,
                message.id,
                discord.utils.utcnow().strftime("%d.%m.%Y %H:%M UTC"),
                verdict_id=verdict_data["id"],
                posts_remaining=verdict_data["consequences"].get("posts_remaining"),
            )
            if verdict_data["state"] == "pending":
                embed.add_field(
                    name="Состояние", value="Ожидает проверки администратора", inline=False
                )
            elif verdict_data["state"] == "engine_rejected":
                embed.add_field(
                    name="Состояние", value="Изменения отклонены игровым движком", inline=False
                )
            sent = None
            try:
                async for candidate in channel.history(limit=50):
                    reference = candidate.reference
                    if (
                        candidate.author.id == bot.user.id
                        and reference is not None
                        and reference.message_id == message.id
                    ):
                        sent = candidate
                        break
            except discord.HTTPException:
                log.exception("Could not inspect recent replies before republishing")
            if sent is None:
                sent = await message.reply(
                    embed=embed,
                    mention_author=False,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            else:
                await sent.edit(embed=embed)
            async with bot.db() as session:
                row = await session.get(Verdict, verdict_data["id"])
                row.message_id = sent.id
                await session.commit()
        await interaction.followup.send(
            "Повторная обработка/публикация завершена. Ответ находится Reply на исходном сообщении.",
            ephemeral=True,
        )

    @post_retry.autocomplete("discord_message_id")
    @admin_check(bot)
    async def post_retry_autocomplete(interaction: discord.Interaction, current: str):
        return await failed_post_choices(interaction, current)

    @bot.tree.command(
        name="post-grant", description="Выдать игроку дополнительные игровые посты на сегодня"
    )
    @app_commands.describe(
        member="Игрок, которому выдать дополнительные посты",
        extra_posts="Число дополнительных постов на текущий игровой день (1–20)",
    )
    @admin_check(bot)
    async def post_grant(
        interaction: discord.Interaction,
        member: discord.Member,
        extra_posts: app_commands.Range[int, 1, 20] = 1,
    ):
        from vpi_bot.models import AuditLog

        config = await bot.guild_config(interaction.guild_id)
        if not config:
            await interaction.response.send_message("Сервер не настроен.", ephemeral=True)
            return
        local_date = discord.utils.utcnow().astimezone(ZoneInfo(config.timezone)).date()
        async with bot.db() as session:
            session.add(
                PostLimitGrant(
                    guild_id=interaction.guild_id,
                    user_id=member.id,
                    local_date=local_date,
                    extra_posts=extra_posts,
                    granted_by=interaction.user.id,
                )
            )
            session.add(
                AuditLog(
                    guild_id=interaction.guild_id,
                    actor_id=interaction.user.id,
                    action="post_grant",
                    target_type="user",
                    target_id=str(member.id),
                    new_value=json.dumps(
                        {"date": local_date.isoformat(), "extra_posts": extra_posts}
                    ),
                    reason="Выдан дополнительный лимит игровых сообщений",
                )
            )
            await session.commit()
        await interaction.response.send_message(
            f"Выдано дополнительных постов: {extra_posts} для {member.mention}.", ephemeral=True
        )

    @bot.tree.command(
        name="economy-adjust", description="Скорректировать экономический показатель государства"
    )
    @app_commands.describe(
        country_id="Выберите государство из списка",
        metric="Экономический показатель для корректировки",
        delta="Изменение: положительное увеличить, отрицательное уменьшить",
        reason="Причина корректировки; будет записана в аудит",
    )
    @app_commands.choices(
        metric=[
            app_commands.Choice(name="Казна", value="treasury"),
            app_commands.Choice(name="ВВП", value="gdp"),
            app_commands.Choice(name="Доходы", value="income"),
            app_commands.Choice(name="Расходы", value="expenses"),
            app_commands.Choice(name="Инфляция", value="inflation"),
            app_commands.Choice(name="Безработица", value="unemployment"),
            app_commands.Choice(name="Экономический рост", value="growth"),
            app_commands.Choice(name="Государственный долг", value="debt"),
            app_commands.Choice(name="Налоговые доходы", value="tax_revenue"),
            app_commands.Choice(name="Промышленность", value="industry_level"),
            app_commands.Choice(name="Стабильность", value="stability"),
        ]
    )
    @admin_check(bot)
    async def economy_adjust(
        interaction: discord.Interaction, country_id: int, metric: str, delta: float, reason: str
    ):
        from vpi_bot.game_engine import ALLOWED_METRICS, BOUNDED_METRICS, MONEY_METRICS
        from vpi_bot.models import AuditLog

        if metric not in ALLOWED_METRICS:
            await interaction.response.send_message(
                "Недопустимый показатель. Допустимые: " + ", ".join(sorted(ALLOWED_METRICS)),
                ephemeral=True,
            )
            return
        async with bot.db() as session:
            country = await session.scalar(
                select(Country)
                .where(Country.id == country_id, Country.guild_id == interaction.guild_id)
                .with_for_update()
            )
            stats = (
                await session.scalar(
                    select(EconomicStats)
                    .where(EconomicStats.country_id == country_id)
                    .with_for_update()
                )
                if country
                else None
            )
            if not country or not stats:
                await interaction.response.send_message("Государство не найдено.", ephemeral=True)
                return
            old = Decimal(str(getattr(stats, metric)))
            change = Decimal(str(delta))
            if metric in MONEY_METRICS:
                change = change.quantize(Decimal("0.01"))
            elif metric in BOUNDED_METRICS:
                change = change.quantize(Decimal("0.001"))
            if metric == "industry_level" and change != change.to_integral_value():
                await interaction.response.send_message(
                    "Уровень промышленности изменяется только целым числом.", ephemeral=True
                )
                return
            new = old + change
            if (
                not new.is_finite()
                or (new < 0 and metric != "growth")
                or (
                    metric in BOUNDED_METRICS
                    and not BOUNDED_METRICS[metric][0] <= new <= BOUNDED_METRICS[metric][1]
                )
            ):
                await interaction.response.send_message(
                    "Изменение нарушает допустимые диапазоны/баланс.", ephemeral=True
                )
                return
            setattr(stats, metric, int(new) if metric == "industry_level" else new)
            session.add(
                AuditLog(
                    guild_id=interaction.guild_id,
                    actor_id=interaction.user.id,
                    action="economy_adjust",
                    target_type="country",
                    target_id=str(country_id),
                    old_value=str(old),
                    new_value=str(new),
                    reason=reason[:1000],
                )
            )
            await session.commit()
        await interaction.response.send_message(
            f"{metric}: {old} → {new}. Причина внесена в журнал.", ephemeral=True
        )

    @economy_adjust.autocomplete("country_id")
    @admin_check(bot)
    async def economy_country_autocomplete(interaction: discord.Interaction, current: str):
        return await country_choices(interaction, current)

    @bot.tree.command(
        name="verdict-amend", description="Отредактировать карточку и последствия вердикта"
    )
    @app_commands.describe(
        verdict_id="ID применённого или ожидающего вердикта",
        description="Новый короткий текст итоговой карточки",
        reason="Причина правки для комментария и аудита",
        changes_json="JSON новых числовых изменений; пусто — оставить текущие",
    )
    @admin_check(bot)
    async def verdict_amend(
        interaction: discord.Interaction,
        verdict_id: int,
        description: str,
        reason: str,
        changes_json: str = "",
    ):
        async with bot.db() as session:
            verdict = await session.get(Verdict, verdict_id)
            post = await session.get(GamePost, verdict.post_id) if verdict else None
            country = (
                await session.scalar(
                    select(Country).where(Country.id == post.country_id).with_for_update()
                )
                if post
                else None
            )
            if not verdict or not post or not country or post.guild_id != interaction.guild_id:
                await interaction.response.send_message(
                    "Вердикт не найден на этом сервере.", ephemeral=True
                )
                return
            if verdict.state not in {"pending", "applied"}:
                await interaction.response.send_message(
                    "Можно редактировать только ожидающий или применённый вердикт.", ephemeral=True
                )
                return
            try:
                new_changes = (
                    ProposedChanges.model_validate_json(changes_json)
                    if changes_json.strip()
                    else ProposedChanges.model_validate(verdict.proposed_changes)
                )
                old_description = verdict.description
                old_changes = verdict.proposed_changes
                if verdict.state == "applied":
                    from vpi_bot.game_engine import reverse_changes

                    await reverse_changes(session, verdict)
                verdict.description = description[:4000]
                verdict.proposed_changes = new_changes.model_dump()
                if verdict.state == "applied":
                    from vpi_bot.game_engine import apply_changes

                    verdict.applied_changes = await apply_changes(
                        session, country, verdict, verdict.proposed_changes
                    )
                verdict.moderator_comment = reason[:1000]
                from vpi_bot.models import AuditLog, HistoricalEvent

                event = await session.scalar(
                    select(HistoricalEvent).where(HistoricalEvent.verdict_id == verdict.id)
                )
                if event:
                    event.title, event.content = verdict.title, verdict.description

                session.add(
                    AuditLog(
                        guild_id=interaction.guild_id,
                        actor_id=interaction.user.id,
                        action="verdict_amend",
                        target_type="verdict",
                        target_id=str(verdict.id),
                        old_value=json.dumps(
                            {"description": old_description, "changes": old_changes},
                            ensure_ascii=False,
                        ),
                        new_value=json.dumps(
                            {
                                "description": verdict.description,
                                "changes": verdict.proposed_changes,
                            },
                            ensure_ascii=False,
                        ),
                        reason=reason[:1000],
                    )
                )
                await session.commit()
                channel_id, reply_id, state, details = (
                    post.channel_id,
                    verdict.message_id,
                    verdict.state,
                    verdict.consequences,
                )
                title, status, reasoning, duration = (
                    verdict.title,
                    verdict.status,
                    verdict.reasoning,
                    verdict.duration_turns,
                )
            except (ChangeRejected, ValueError) as exc:
                await session.rollback()
                await interaction.response.send_message(
                    f"Правка не применена: {exc}", ephemeral=True
                )
                return
        if channel_id and reply_id:
            channel = bot.get_channel(channel_id)
            if channel is None:
                try:
                    channel = await bot.fetch_channel(channel_id)
                except discord.HTTPException:
                    channel = None
            if channel:
                try:
                    card = await channel.fetch_message(reply_id)
                    if card.embeds:
                        result = JudgeResult(
                            is_game_action=True,
                            status=status,
                            title=title,
                            description=description[:4000],
                            reasoning=reasoning,
                            positives=details.get("positives", []),
                            negatives=details.get("negatives", []),
                            conditions=details.get("conditions", []),
                            changes=new_changes,
                            duration_turns=duration,
                        )
                        embed = verdict_embed(
                            result,
                            country,
                            post.discord_message_id,
                            discord.utils.utcnow().strftime("%d.%m.%Y %H:%M UTC"),
                            verdict_id=verdict.id,
                            posts_remaining=details.get("posts_remaining"),
                        )
                        embed.add_field(
                            name="Состояние",
                            value=(
                                "Изменения применены (с учётом правки)"
                                if state == "applied"
                                else "Ожидает утверждения модератором"
                            ),
                            inline=False,
                        )
                        await card.edit(embed=embed)
                except discord.HTTPException:
                    log.exception("Could not update amended verdict card")
        await interaction.response.send_message(
            "Вердикт и его карточка обновлены; исходная версия записана в аудит.",
            ephemeral=True,
        )

    @verdict_amend.autocomplete("verdict_id")
    @admin_check(bot)
    async def amend_verdict_autocomplete(interaction: discord.Interaction, current: str):
        return await verdict_choices(interaction, current, states=("pending", "applied"))

    @bot.tree.command(name="verdict-cancel", description="Отменить применённые изменения вердикта")
    @app_commands.describe(
        verdict_id="ID применённого вердикта, который требуется отменить",
        reason="Причина отмены для карточки и аудита",
    )
    @admin_check(bot)
    async def verdict_cancel(interaction: discord.Interaction, verdict_id: int, reason: str):
        from vpi_bot.game_engine import reverse_changes

        async with bot.db() as session:
            verdict = await session.get(Verdict, verdict_id)
            post = await session.get(GamePost, verdict.post_id) if verdict else None
            if (
                not verdict
                or not post
                or post.guild_id != interaction.guild_id
                or verdict.state != "applied"
            ):
                await interaction.response.send_message(
                    "Применённый вердикт не найден.", ephemeral=True
                )
                return
            try:
                await reverse_changes(session, verdict)
            except ChangeRejected as exc:
                await session.rollback()
                await interaction.response.send_message(
                    f"Безопасная отмена невозможна: {exc}", ephemeral=True
                )
                return
            verdict.state, verdict.moderator_comment = "cancelled", reason[:1000]
            from vpi_bot.models import AuditLog

            session.add(
                AuditLog(
                    guild_id=interaction.guild_id,
                    actor_id=interaction.user.id,
                    action="verdict_cancel",
                    target_type="verdict",
                    target_id=str(verdict.id),
                    old_value="applied",
                    new_value="cancelled",
                    reason=reason[:1000],
                )
            )
            channel_id, message_id = post.channel_id, verdict.message_id
            await session.commit()
        from vpi_bot.bot import update_verdict_card

        await update_verdict_card(
            bot, channel_id, message_id, "Изменения отменены администратором", reason
        )
        await interaction.response.send_message(
            "Изменения вердикта отменены; исходный вердикт сохранён в журнале.", ephemeral=True
        )

    @verdict_cancel.autocomplete("verdict_id")
    @admin_check(bot)
    async def cancel_verdict_autocomplete(interaction: discord.Interaction, current: str):
        return await verdict_choices(interaction, current, states=("applied",))
