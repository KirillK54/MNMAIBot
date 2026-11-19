from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vpi_bot.models import GamePost


async def reclaim_failed_post(
    session: AsyncSession,
    *,
    guild_id: int,
    discord_message_id: int,
    country_id: int,
    channel_id: int,
    body: str,
    local_date: date,
) -> int | None:
    """Atomically reset a failed message for retry; concurrent workers can claim it only once."""
    post = await session.scalar(
        select(GamePost)
        .where(
            GamePost.guild_id == guild_id,
            GamePost.discord_message_id == discord_message_id,
        )
        .with_for_update()
    )
    if post is None or post.state != "failed":
        return None
    post.state = "received"
    post.processing_started_at = None
    post.body = body[:12000]
    post.country_id = country_id
    post.channel_id = channel_id
    post.local_date = local_date
    await session.flush()
    return post.id
