from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vpi_bot.models import HistoricalEvent, LoreSummary


async def refresh_event_summary(session: AsyncSession, country_id: int) -> bool:
    """Create a compact extractive timeline every ten new important events."""
    summary = await session.scalar(
        select(LoreSummary)
        .where(LoreSummary.country_id == country_id)
        .order_by(LoreSummary.updated_at.desc())
        .with_for_update()
    )
    through_id = summary.through_event_id or 0 if summary else 0
    events = (
        await session.scalars(
            select(HistoricalEvent)
            .where(HistoricalEvent.country_id == country_id, HistoricalEvent.id > through_id)
            .order_by(HistoricalEvent.id)
            .limit(10)
        )
    ).all()
    if len(events) < 10:
        return False
    lines = []
    for event in events:
        excerpt = " ".join(event.content.split())[:220]
        lines.append(
            f"[{event.event_date:%Y-%m-%d}; значимость {event.importance}] {event.title}: {excerpt}"
        )
    prior = summary.content.splitlines() if summary and summary.content else []
    combined = prior + lines
    # Keep a bounded, chronological digest of the forty latest major events.
    content = "\n".join(combined[-40:])[:12000]
    if summary is None:
        summary = LoreSummary(
            country_id=country_id, content=content, through_event_id=events[-1].id
        )
        session.add(summary)
    else:
        summary.content = content
        summary.through_event_id = events[-1].id
    await session.flush()
    return True
