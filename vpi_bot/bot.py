import asyncio
import logging
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands, tasks
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from vpi_bot.ai.provider import OpenAICompatibleProvider
from vpi_bot.config import Settings
from vpi_bot.game_engine import ChangeRejected, apply_changes
from vpi_bot.judge import judge_post
from vpi_bot.models import (
    Base,
    Country,
    DailyRun,
    GamePost,
    GuildConfig,
    HistoricalEvent,
    PostLimitGrant,
    ResourceProduction,
    Verdict,
)
from vpi_bot.outbox import claim_due, enqueue_verdict, mark_failed, mark_sent
from vpi_bot.security import authorized_guild
from vpi_bot.ui.cards import verdict_embed

log = logging.getLogger(__name__)


async def update_verdict_card(
    bot, channel_id: int | None, message_id: int | None, state: str, note: str | None = None
) -> None:
    """Edit the existing reply card; administrative actions never create a replacement post."""
    if not channel_id or not message_id:
        return
    channel = bot.get_channel(channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(channel_id)
        except discord.HTTPException:
            return
    try:
        card = await channel.fetch_message(message_id)
        if not card.embeds:
            return
        embed = card.embeds[0]
        index = next((i for i, field in enumerate(embed.fields) if field.name == "Состояние"), None)
        if index is None:
            embed.add_field(name="Состояние", value=state, inline=False)
        else:
            embed.set_field_at(index, name="Состояние", value=state, inline=False)
        if note:
            embed.description = (embed.description or "")[
                :3500
            ] + f"\n\n🛡️ Примечание администратора: {note[:800]}"
        await card.edit(embed=embed)
    except (discord.HTTPException, AttributeError):
        log.exception("Unable to update existing verdict card %s", message_id)


def is_permanent_outbox_error(error: Exception) -> bool:
    if isinstance(error, discord.NotFound):
        return True
    return (
        isinstance(error, discord.HTTPException)
        and error.status is not None
        and error.status not in {408, 425, 429}
        and 400 <= error.status < 500
    )


async def send_daily_report(
    channel, channel_id: int, country_id: int, embed: discord.Embed
) -> None:
    try:
        await channel.send(embed=embed)
    except discord.Forbidden:
        log.warning(
            "Missing Access while sending daily report to channel %s for country %s; "
            "verify bot membership and grant View Channel, Send Messages and Embed Links",
            channel_id,
            country_id,
        )
    except discord.NotFound:
        log.warning(
            "Daily-report channel %s for country %s no longer exists", channel_id, country_id
        )
    except discord.HTTPException as exc:
        log.warning(
            "Daily report for country %s failed with Discord HTTP %s", country_id, exc.status
        )


async def record_engine_rejection(
    bot, post_id, country_id, result, reason, tz, posts_remaining: int
) -> bool:
    async with bot.db() as session:
        post = await session.get(GamePost, post_id)
        country = await session.get(Country, country_id)
        if post is None or country is None:
            return False
        description = (
            result.description
            + "\n\nИгровой движок отклонил предложенные изменения: "
            + str(reason)
        )[:4000]
        rejected_result = result.model_copy(
            update={"status": "rejected", "description": description}
        )
        post.state = "engine_rejected"
        verdict = Verdict(
            post_id=post.id,
            status=result.status,
            title=result.title,
            description=description,
            reasoning=result.reasoning,
            consequences={
                "positives": result.positives,
                "negatives": result.negatives,
                "conditions": result.conditions,
                "posts_remaining": posts_remaining,
            },
            proposed_changes=result.changes.model_dump(),
            applied_changes={},
            duration_turns=result.duration_turns,
            state="engine_rejected",
        )
        session.add(verdict)
        await session.flush()
        session.add(
            HistoricalEvent(
                verdict_id=verdict.id,
                country_id=country.id,
                title=result.title,
                content=description,
                importance=5,
                tags=[result.status, "engine_rejected"],
            )
        )
        from vpi_bot.lore import refresh_event_summary

        await refresh_event_summary(session, country.id)
        embed = build_verdict_embed(
            rejected_result,
            country,
            post,
            verdict.state,
            datetime.now(tz).strftime("%d.%m.%Y %H:%M %Z"),
            verdict_id=verdict.id,
            posts_remaining=posts_remaining,
        )
        await enqueue_verdict(session, post, verdict, embed.to_dict())
        await session.commit()
    return True


def build_verdict_embed(
    result,
    country: Country,
    post: GamePost,
    state: str,
    timestamp: str,
    *,
    verdict_id: int | None = None,
    posts_remaining: int | None = None,
):
    embed = verdict_embed(
        result,
        country,
        post.discord_message_id,
        timestamp,
        verdict_id=verdict_id,
        posts_remaining=posts_remaining,
    )
    if state == "pending":
        embed.add_field(name="Состояние", value="Ожидает проверки администратора", inline=False)
    elif state == "engine_rejected":
        embed.add_field(name="Состояние", value="Изменения отклонены игровым движком", inline=False)
    return embed


class VPIBot(commands.Bot):
    def __init__(self, settings: Settings, session_factory):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        super().__init__(
            command_prefix="!", intents=intents, application_id=settings.discord_application_id
        )
        self.settings, self.db = settings, session_factory
        self.engine = None
        self.ai = OpenAICompatibleProvider(settings)
        self.active_message_ids: set[int] = set()
        self._outbox_lock = asyncio.Lock()

        @self.command(name="verdict", help="Отправить пост на вердикт в своём канале")
        async def verdict_command(ctx: commands.Context, *, post_text: str = ""):
            await self._submit_verdict_command(ctx, post_text)

    async def setup_hook(self) -> None:
        from vpi_bot.database import engine

        self.engine = engine
        try:
            from vpi_bot.db_bootstrap import ensure_database_exists

            if await ensure_database_exists(engine.url):
                log.info("Created PostgreSQL database %s", engine.url.database)
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
        except (OSError, SQLAlchemyError) as exc:
            log.error("Database startup check failed: %s", exc)
            raise SystemExit(
                "База данных недоступна или схема не подготовлена. Проверьте DATABASE_URL, "
                "запущенный PostgreSQL и права пользователя; для локального Compose запустите "
                "`docker compose up -d db`."
            ) from None
        await self.tree.sync()
        self.resource_tick.start()
        self.daily_economy.start()
        self.outbox_dispatch.start()

    async def close(self) -> None:
        pending = []
        for loop in (self.outbox_dispatch, self.resource_tick, self.daily_economy):
            task = loop.get_task()
            if task is not None and not task.done():
                loop.cancel()
                pending.append(task)
        try:
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            await super().close()
        finally:
            if self.engine is not None:
                await self.engine.dispose()

    async def guild_config(self, guild_id: int | None) -> GuildConfig | None:
        if guild_id is None:
            return None
        async with self.db() as session:
            return await authorized_guild(session, guild_id, self.settings)

    async def flush_outbox(self, limit: int = 20) -> None:
        async with self._outbox_lock:
            async with self.db() as session:
                deliveries = await claim_due(session, limit=limit, lease_seconds=300)
                await session.commit()
            for delivery in deliveries:
                try:
                    channel = self.get_channel(delivery.channel_id)
                    if channel is None:
                        channel = await self.fetch_channel(delivery.channel_id)
                    source = await channel.fetch_message(delivery.source_message_id)
                    sent = None
                    if self.user is not None:
                        async for candidate in channel.history(limit=100):
                            reference = candidate.reference
                            footer = (
                                candidate.embeds[0].footer.text or ""
                                if candidate.embeds and candidate.embeds[0].footer
                                else ""
                            )
                            if (
                                candidate.author.id == self.user.id
                                and reference is not None
                                and reference.message_id == source.id
                                and f"пост #{source.id}" in footer.casefold()
                            ):
                                sent = candidate
                                break
                    if sent is None:
                        sent = await source.reply(
                            embed=discord.Embed.from_dict(delivery.embed_payload),
                            mention_author=False,
                            allowed_mentions=discord.AllowedMentions.none(),
                        )
                    async with self.db() as session:
                        await mark_sent(session, delivery.id, sent.id)
                        await session.commit()
                except Exception as exc:  # noqa: BLE001 - Discord and DB failures must be persisted for retry.
                    log.warning(
                        "Verdict reply outbox %s failed on attempt %s: %s",
                        delivery.id,
                        delivery.attempts,
                        exc,
                    )
                    permanent = is_permanent_outbox_error(exc)
                    try:
                        async with self.db() as session:
                            await mark_failed(session, delivery.id, str(exc), permanent=permanent)
                            await session.commit()
                    except Exception:
                        log.exception("Could not persist outbox failure %s", delivery.id)

    @tasks.loop(seconds=30)
    async def outbox_dispatch(self):
        await self.flush_outbox()

    @outbox_dispatch.before_loop
    async def before_outbox_dispatch(self):
        await self.wait_until_ready()

    @outbox_dispatch.error
    async def outbox_dispatch_error(self, error: Exception):
        log.exception("Outbox dispatch loop failed", exc_info=error)

    async def on_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        original = getattr(error, "original", error)
        if isinstance(error, app_commands.CheckFailure):
            text = "Команда недоступна: проверьте права администратора и настройку сервера."
        elif isinstance(error, app_commands.CommandOnCooldown):
            text = f"Слишком часто. Повторите через {error.retry_after:.1f} с."
        elif isinstance(original, (ValueError, KeyError)):
            text = f"Не удалось выполнить действие: {original}"
        else:
            log.error(
                "Application command failed",
                exc_info=(type(original), original, original.__traceback__),
            )
            text = "Не удалось выполнить команду. Ошибка записана в журнал; попробуйте позже."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(text, ephemeral=True)
            else:
                await interaction.response.send_message(text, ephemeral=True)
        except discord.HTTPException:
            log.exception("Could not send application command error to Discord")

    async def on_message(self, message: discord.Message) -> None:
        # Posts are submitted explicitly with !verdict; ordinary chat must not consume turns.
        await self.process_commands(message)

    async def _submit_verdict_command(self, ctx: commands.Context, post_text: str = "") -> None:
        if ctx.guild is None:
            await ctx.reply("Команда доступна только в игровом сервере.", mention_author=False)
            return
        if post_text.strip():
            source_message = ctx.message
            body = post_text.strip()
        else:
            reference = ctx.message.reference
            if reference is None or reference.message_id is None:
                await ctx.reply(
                    "Напишите `!verdict Текст поста` или ответьте `!verdict` на своё сообщение.",
                    mention_author=False,
                )
                return
            try:
                source_message = reference.resolved
                if not isinstance(source_message, discord.Message):
                    source_message = await ctx.channel.fetch_message(reference.message_id)
            except discord.HTTPException:
                await ctx.reply(
                    "Не удалось найти сообщение, на которое вы ответили.", mention_author=False
                )
                return
            if (
                source_message.author.id != ctx.author.id
                or source_message.channel.id != ctx.channel.id
            ):
                await ctx.reply(
                    "Отправлять на вердикт можно только свой пост в этом канале.",
                    mention_author=False,
                )
                return
            body = source_message.content.strip()
        if not body:
            await ctx.reply(
                "Текст поста пуст. Добавьте текст и повторите команду.", mention_author=False
            )
            return
        if len(body) > 2000:
            await ctx.reply(
                "Пост превышает лимит 2000 символов. Сократите его и повторите.",
                mention_author=False,
            )
            return
        await self.process_game_post(source_message, body)

    async def process_game_post(self, message: discord.Message, body: str | None = None) -> bool:
        post_body = (body if body is not None else message.content).strip()
        if not post_body or len(post_body) > 2000 or message.id in self.active_message_ids:
            return False
        self.active_message_ids.add(message.id)
        try:
            await self._process_message(message, post_body)
        finally:
            self.active_message_ids.discard(message.id)
        return True

    async def _process_message(self, message: discord.Message, body: str | None = None) -> None:
        post_body = (body if body is not None else message.content).strip()
        if message.author.bot or message.guild is None or not post_body:
            return
        if len(post_body) > 2000:
            try:
                await message.reply("Пост превышает лимит 2000 символов.", mention_author=False)
            except discord.HTTPException:
                pass
            return
        config = await self.guild_config(message.guild.id)
        if config is None:
            try:
                await message.reply("Сервер ещё не настроен администратором.", mention_author=False)
            except discord.HTTPException:
                pass
            return
        parent_channel_id = (
            message.channel.parent_id
            if isinstance(message.channel, discord.Thread)
            else message.channel.id
        )
        async with self.db() as session:
            country = await session.scalar(
                select(Country).where(
                    Country.guild_id == message.guild.id,
                    Country.channel_id == parent_channel_id,
                    Country.owner_id == message.author.id,
                    Country.is_active.is_(True),
                )
            )
            if country is None:
                try:
                    await message.reply(
                        "Команда `!verdict` работает только в приватном канале вашего государства.",
                        mention_author=False,
                    )
                except discord.HTTPException:
                    pass
                return
            country_id = country.id
            tz = ZoneInfo(config.timezone)
            local_date = datetime.now(tz).date()
            post = GamePost(
                guild_id=message.guild.id,
                discord_message_id=message.id,
                channel_id=message.channel.id,
                author_id=message.author.id,
                country_id=country_id,
                body=post_body,
                local_date=local_date,
                state="received",
            )
            session.add(post)
            try:
                await session.commit()
                post_id = post.id
            except IntegrityError:
                await session.rollback()
                from vpi_bot.post_processing import reclaim_failed_post

                post_id = await reclaim_failed_post(
                    session,
                    guild_id=message.guild.id,
                    discord_message_id=message.id,
                    country_id=country_id,
                    channel_id=message.channel.id,
                    body=post_body,
                    local_date=local_date,
                )
                if post_id is None:
                    return
                await session.commit()
        outbox_committed = False
        try:
            await message.add_reaction("⏳")
            async with self.db() as session:
                country = await session.get(Country, country_id)
                post = await session.get(GamePost, post_id)
                post.state = "processing"
                post.processing_started_at = discord.utils.utcnow()
                post.attempts += 1
                await session.commit()
                result, verdict = await judge_post(
                    session, self.ai, post, country, config, apply_automatically=False
                )
                if not result.is_game_action:
                    await session.commit()
                    await message.remove_reaction("⏳", self.user)
                    return
                # Serialize this country's limit and financial mutations without holding a lock during the AI call.
                country = await session.scalar(
                    select(Country).where(Country.id == country_id).with_for_update()
                )
                day_count = await session.scalar(
                    select(func.count(GamePost.id)).where(
                        GamePost.guild_id == message.guild.id,
                        GamePost.author_id == message.author.id,
                        GamePost.local_date == local_date,
                        GamePost.state.in_(["judged", "pending", "engine_rejected"]),
                        GamePost.id != post.id,
                    )
                )
                grants = await session.scalar(
                    select(func.coalesce(func.sum(PostLimitGrant.extra_posts), 0)).where(
                        PostLimitGrant.guild_id == message.guild.id,
                        PostLimitGrant.user_id == message.author.id,
                        PostLimitGrant.local_date == local_date,
                    )
                )
                post_limit = config.daily_post_limit + int(grants or 0)
                if day_count >= post_limit:
                    verdict.state = "rejected"
                    post.state = "over_limit"
                    await session.commit()
                    await message.remove_reaction("⏳", self.user)
                    await message.add_reaction("❌")
                    await message.reply(
                        "Дневной лимит постов исчерпан: осталось 0.", mention_author=False
                    )
                    return
                posts_remaining = max(post_limit - day_count - 1, 0)
                verdict.consequences = {
                    **(verdict.consequences or {}),
                    "posts_remaining": posts_remaining,
                }
                old_changes = None
                try:
                    if config.verdict_mode == "automatic":
                        old_changes = await apply_changes(
                            session, country, verdict, result.changes.model_dump()
                        )
                        verdict.applied_changes = old_changes
                        verdict.state = "applied"
                        post.state = "judged"
                        from vpi_bot.ai.schemas import MilitaryChanges
                        from vpi_bot.lore import refresh_event_summary

                        result.changes = result.changes.model_copy(
                            update={
                                "metrics": old_changes["metrics"],
                                "resources": old_changes["resources"],
                                "military": MilitaryChanges.model_validate(old_changes["military"]),
                            }
                        )
                        completion_notes = []
                        if old_changes.get("completed_projects"):
                            completion_notes.append(
                                "Завершены строительные проекты: "
                                + ", ".join(old_changes["completed_projects"])
                                + "."
                            )
                        if old_changes.get("completed_research"):
                            completion_notes.append(
                                "Завершены исследования: "
                                + ", ".join(old_changes["completed_research"])
                                + "."
                            )
                        if completion_notes:
                            result.description = (
                                result.description + "\n\n" + "\n".join(completion_notes)
                            )[:4000]
                            verdict.description = result.description
                        await refresh_event_summary(session, country_id)
                    else:
                        verdict.state = "pending"
                        post.state = "pending"
                        event_text = "Ожидается утверждение администратора"
                        result = result.model_copy(
                            update={
                                "status": "conditions_required",
                                "description": (result.description + "\n\n" + event_text)[:4000],
                                "conditions": [*result.conditions, event_text][:8],
                            }
                        )
                except ChangeRejected as exc:
                    await session.rollback()
                    outbox_committed = await record_engine_rejection(
                        self, post_id, country_id, result, exc, tz, posts_remaining
                    )
                    country = await session.get(Country, country_id)
                    verdict = await session.scalar(
                        select(Verdict).where(Verdict.post_id == post_id)
                    )
                    if not outbox_committed:
                        await message.reply(
                            f"Решение не применено: {exc}. Сообщите администратору.",
                            mention_author=False,
                        )
                        return
                if not outbox_committed:
                    embed = build_verdict_embed(
                        result,
                        country,
                        post,
                        verdict.state,
                        datetime.now(tz).strftime("%d.%m.%Y %H:%M %Z"),
                        verdict_id=verdict.id,
                        posts_remaining=posts_remaining,
                    )
                    await enqueue_verdict(session, post, verdict, embed.to_dict())
                    await session.commit()
                    outbox_committed = True
                await message.remove_reaction("⏳", self.user)
                await message.add_reaction("✅" if verdict.state == "applied" else "⚖️")
            await self.flush_outbox()
        except Exception:
            log.exception("Post processing failed: %s", message.id)
            try:
                async with self.db() as recovery:
                    stored_post = await recovery.scalar(
                        select(GamePost).where(GamePost.discord_message_id == message.id)
                    )
                    if stored_post and stored_post.state in {"received", "processing"}:
                        stored_post.state = "failed"
                        await recovery.commit()
            except Exception:
                log.exception("Failed to persist retryable post failure: %s", message.id)
            try:
                await message.remove_reaction("⏳", self.user)
                await message.add_reaction("❌")
                await message.reply(
                    "Не удалось обработать действие. Оно сохранено; сообщите модератору для повторной обработки.",
                    mention_author=False,
                )
            except discord.HTTPException:
                pass

    @tasks.loop(hours=1)
    async def resource_tick(self):
        try:
            await self._resource_tick_once()
        except Exception:
            log.exception("Resource production sweep failed")

    async def _resource_tick_once(self):
        # Candidate IDs are read without locks; each cycle locks and commits independently.
        from vpi_bot.models import IndustryFacility

        async with self.db() as session:
            production_ids = (
                await session.scalars(
                    select(ResourceProduction.id)
                    .where(ResourceProduction.enabled.is_(True))
                    .order_by(ResourceProduction.country_id, ResourceProduction.id)
                )
            ).all()
            facility_ids = (
                await session.scalars(
                    select(IndustryFacility.id)
                    .where(IndustryFacility.status == "active")
                    .order_by(IndustryFacility.id)
                )
            ).all()
        now = discord.utils.utcnow()
        change_lines: dict[int, list[str]] = {}
        from vpi_bot.production import run_resource_production_cycle

        for production_id in production_ids:
            try:
                async with self.db() as session:
                    report = await run_resource_production_cycle(session, production_id, now=now)
                    await session.commit()
                if report:
                    change_lines.setdefault(report["country_id"], []).append(
                        f"{report['resource_name']}: +{report['amount']} "
                        f"({report['before']} → {report['after']})"
                    )
            except ChangeRejected as exc:
                log.warning("Skipping invalid resource production %s: %s", production_id, exc)
            except Exception:
                log.exception("Resource production cycle %s failed", production_id)

        from vpi_bot.industry import run_facility_cycle

        for facility_id in facility_ids:
            try:
                async with self.db() as session:
                    facility = await session.get(IndustryFacility, facility_id)
                    report = (
                        await run_facility_cycle(session, facility, now=now) if facility else None
                    )
                    await session.commit()
                if report:
                    change_lines.setdefault(facility.country_id, []).append(
                        f"🏭 {report['facility']}: выпуск {report['outputs']} · "
                        f"расход {report['inputs']} · эксплуатация {report['cost']}"
                    )
            except ChangeRejected as exc:
                log.warning("Skipping invalid industry facility %s: %s", facility_id, exc)
            except Exception:
                log.exception("Industry facility cycle %s failed", facility_id)

        if not change_lines:
            return
        async with self.db() as session:
            countries = (
                await session.scalars(select(Country).where(Country.id.in_(change_lines)))
            ).all()
            destinations = [(country.id, country.name, country.channel_id) for country in countries]
        for country_id, country_name, channel_id in destinations:
            if not channel_id:
                continue
            channel = self.get_channel(channel_id)
            if not channel:
                continue
            embed = discord.Embed(
                title=f"⛏️ Производственный отчёт — {country_name}",
                description="\n".join(change_lines[country_id])[:4000],
                color=0x2ECC71,
            )
            try:
                await channel.send(embed=embed)
            except discord.HTTPException:
                log.exception("Could not publish production report for country %s", country_id)

    @resource_tick.before_loop
    async def before_resource_tick(self):
        await self.wait_until_ready()

    @tasks.loop(minutes=5)
    async def daily_economy(self):
        try:
            await self._daily_economy_once()
        except Exception:
            log.exception("Daily economy sweep failed")

    async def _daily_economy_once(self):
        from vpi_bot.economy import calculate_daily_economy

        async with self.db() as session:
            configs = (await session.scalars(select(GuildConfig))).all()
        now = discord.utils.utcnow()
        for config in configs:
            try:
                local_date = now.astimezone(ZoneInfo(config.timezone)).date()
                async with self.db() as session:
                    country_ids = (
                        await session.scalars(
                            select(Country.id)
                            .where(
                                Country.guild_id == config.guild_id,
                                Country.is_active.is_(True),
                            )
                            .order_by(Country.id)
                        )
                    ).all()
            except Exception:
                log.exception(
                    "Could not list countries for daily economy guild %s", config.guild_id
                )
                continue
            for country_id in country_ids:
                try:
                    async with self.db() as session:
                        country = await session.get(Country, country_id)
                        if country is None or not country.is_active:
                            continue
                        already_run = await session.scalar(
                            select(DailyRun.id).where(
                                DailyRun.country_id == country.id,
                                DailyRun.local_date == local_date,
                                DailyRun.process == "economy",
                            )
                        )
                        if already_run:
                            continue
                        country_name, currency = country.name, country.currency
                        report_channel_ids = {
                            channel_id
                            for channel_id in (country.channel_id, config.reports_channel_id)
                            if channel_id is not None
                        }
                        report = await calculate_daily_economy(session, country, local_date)
                        await session.commit()
                    if not report:
                        continue
                    embed = discord.Embed(
                        title=f"📊 Ежедневный экономический отчёт — {country_name}",
                        color=0x2ECC71 if Decimal(report["deficit"]) == 0 else 0xE67E22,
                    )
                    embed.add_field(
                        name="Казна",
                        value=f"{report['treasury_before']} → {report['treasury_after']} {currency}",
                        inline=False,
                    )
                    embed.add_field(
                        name="Доходы / расходы",
                        value=f"+{report['income']} / −{report['expenses']} {currency}",
                        inline=True,
                    )
                    embed.add_field(
                        name="ВВП",
                        value=f"{report['gdp_before']} → {report['gdp_after']}",
                        inline=True,
                    )
                    embed.add_field(
                        name="Инфляция / безработица",
                        value=f"{report['inflation']}% / {report['unemployment']}%",
                        inline=True,
                    )
                    if Decimal(report["deficit"]) > 0:
                        embed.add_field(
                            name="Непокрытый дефицит",
                            value=f"{report['deficit']} {currency}",
                            inline=True,
                        )
                    for channel_id in report_channel_ids:
                        channel = self.get_channel(channel_id)
                        if channel is None:
                            try:
                                channel = await self.fetch_channel(channel_id)
                            except discord.Forbidden:
                                log.warning(
                                    "Cannot access daily-report channel %s for country %s; "
                                    "verify bot membership and grant View Channel, Send Messages "
                                    "and Embed Links",
                                    channel_id,
                                    country_id,
                                )
                                continue
                            except discord.NotFound:
                                log.warning(
                                    "Daily-report channel %s for country %s no longer exists",
                                    channel_id,
                                    country_id,
                                )
                                continue
                            except discord.HTTPException as exc:
                                log.warning(
                                    "Could not fetch daily-report channel %s (HTTP %s)",
                                    channel_id,
                                    exc.status,
                                )
                                continue
                        await send_daily_report(channel, channel_id, country_id, embed)
                except Exception:
                    log.exception("Daily economy settlement failed for country %s", country_id)

    @daily_economy.before_loop
    async def before_daily_economy(self):
        await self.wait_until_ready()


def install_commands(bot: VPIBot) -> None:
    """Backward-compatible lazy import for the modular command installer."""
    from vpi_bot.commands import install_commands as register_commands

    register_commands(bot)
