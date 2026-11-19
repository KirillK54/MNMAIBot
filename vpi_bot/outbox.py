from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from vpi_bot.models import GamePost, Verdict, VerdictOutbox, now_utc


@dataclass(frozen=True)
class OutboxDelivery:
    id: int
    post_id: int
    channel_id: int
    source_message_id: int
    embed_payload: dict
    attempts: int


async def enqueue_verdict(
    session: AsyncSession, post: GamePost, verdict: Verdict, embed_payload: dict
) -> int:
    """Persist a reply payload in the same transaction as its verdict."""
    row = await session.scalar(
        select(VerdictOutbox).where(VerdictOutbox.post_id == post.id).with_for_update()
    )
    if row is None:
        row = VerdictOutbox(
            post_id=post.id,
            verdict_id=verdict.id,
            guild_id=post.guild_id,
            channel_id=post.channel_id,
            source_message_id=post.discord_message_id,
            embed_payload=embed_payload,
            status="pending",
            next_attempt_at=now_utc(),
        )
        session.add(row)
        await session.flush()
    elif row.status != "sent":
        row.embed_payload = embed_payload
        row.status = "pending"
        row.next_attempt_at = now_utc()
        row.lease_until = None
        await session.flush()
    return row.id


async def claim_due(
    session: AsyncSession, *, limit: int = 10, lease_seconds: int = 300
) -> list[OutboxDelivery]:
    now = now_utc()
    rows = (
        await session.scalars(
            select(VerdictOutbox)
            .where(
                or_(
                    (VerdictOutbox.status == "pending") & (VerdictOutbox.next_attempt_at <= now),
                    (VerdictOutbox.status == "delivering") & (VerdictOutbox.lease_until <= now),
                )
            )
            .order_by(VerdictOutbox.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
    ).all()
    result = []
    for row in rows:
        row.status = "delivering"
        row.attempts += 1
        row.lease_until = now + timedelta(seconds=lease_seconds)
        result.append(
            OutboxDelivery(
                id=row.id,
                post_id=row.post_id,
                channel_id=row.channel_id,
                source_message_id=row.source_message_id,
                embed_payload=dict(row.embed_payload),
                attempts=row.attempts,
            )
        )
    return result


async def mark_sent(session: AsyncSession, outbox_id: int, discord_message_id: int) -> bool:
    row = await session.scalar(
        select(VerdictOutbox).where(VerdictOutbox.id == outbox_id).with_for_update()
    )
    if row is None:
        return False
    if row.status == "sent":
        return row.delivered_message_id == discord_message_id
    row.status = "sent"
    row.delivered_message_id = discord_message_id
    row.lease_until = None
    row.last_error = None
    verdict = await session.get(Verdict, row.verdict_id)
    if verdict is not None:
        verdict.message_id = discord_message_id
    return True


async def mark_failed(
    session: AsyncSession, outbox_id: int, error: str, *, permanent: bool = False
) -> None:
    row = await session.scalar(
        select(VerdictOutbox).where(VerdictOutbox.id == outbox_id).with_for_update()
    )
    if row is None or row.status == "sent":
        return
    now = now_utc()
    row.status = "dead" if permanent else "pending"
    row.lease_until = None
    row.next_attempt_at = (
        now if permanent else now + timedelta(seconds=min(300, 2 ** min(row.attempts, 8)))
    )
    row.last_error = error[:1000]


async def request_retry(session: AsyncSession, post_id: int) -> int | None:
    row = await session.scalar(
        select(VerdictOutbox).where(VerdictOutbox.post_id == post_id).with_for_update()
    )
    if row is None or row.status == "sent":
        return None
    now = now_utc()
    lease_until = row.lease_until
    if lease_until and lease_until.tzinfo is None:
        lease_until = lease_until.replace(tzinfo=now.tzinfo)
    if row.status == "delivering" and lease_until and lease_until > now:
        return row.id
    row.status = "pending"
    row.next_attempt_at = now
    row.lease_until = None
    row.last_error = None
    return row.id
