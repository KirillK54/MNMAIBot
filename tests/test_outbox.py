import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import discord
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from vpi_bot.bot import VPIBot, is_permanent_outbox_error
from vpi_bot.models import Base, Country, GamePost, Verdict, VerdictOutbox
from vpi_bot.outbox import claim_due, enqueue_verdict, mark_failed, mark_sent, request_retry


async def make_outbox(tmp_path, name="outbox.db", source_id=100, payload=None):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / name}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        country = Country(
            guild_id=5,
            owner_id=6,
            name="Тест",
            government="Республика",
            religion="Нет",
            currency="рубль",
        )
        session.add(country)
        await session.flush()
        post = GamePost(
            guild_id=5,
            discord_message_id=source_id,
            channel_id=200,
            author_id=6,
            country_id=country.id,
            body="Действие",
            local_date=datetime.now(UTC).date(),
        )
        session.add(post)
        await session.flush()
        verdict = Verdict(
            post_id=post.id,
            status="success",
            title="Решение",
            description="Принято",
            proposed_changes={},
            applied_changes={},
            state="applied",
        )
        session.add(verdict)
        await session.flush()
        outbox_id = await enqueue_verdict(
            session,
            post,
            verdict,
            payload or {"title": "Решение", "footer": {"text": f"пост #{source_id}"}},
        )
        await session.commit()
    return engine, factory, outbox_id, post.id, verdict.id


class FakeChannel:
    def __init__(self, source, history_messages=()):
        self.source = source
        self.history_messages = list(history_messages)

    async def fetch_message(self, message_id):
        assert message_id == self.source.id
        return self.source

    async def history(self, limit):
        assert limit == 100
        for message in self.history_messages:
            yield message


def fake_bot(factory, channel):
    return SimpleNamespace(
        _outbox_lock=asyncio.Lock(),
        db=factory,
        get_channel=lambda channel_id: channel,
        user=SimpleNamespace(id=99),
    )


@pytest.mark.asyncio
async def test_outbox_claim_retry_and_delivery_are_durable(tmp_path):
    engine, factory, outbox_id, post_id, verdict_id = await make_outbox(tmp_path)
    async with factory() as session:
        claimed = await claim_due(session, limit=10, lease_seconds=60)
        assert len(claimed) == 1 and claimed[0].attempts == 1
        assert await request_retry(session, post_id) == outbox_id
        await session.flush()
        assert (await session.get(VerdictOutbox, outbox_id)).status == "delivering"
        await session.commit()
        await mark_failed(session, outbox_id, "temporary Discord outage")
        await session.commit()
        row = await session.get(VerdictOutbox, outbox_id)
        assert row.status == "pending" and row.last_error == "temporary Discord outage"
        assert await claim_due(session) == []
        await session.commit()
        assert await request_retry(session, post_id) == outbox_id
        await session.commit()
        claimed = await claim_due(session)
        assert len(claimed) == 1 and claimed[0].attempts == 2
        await session.commit()
        assert await mark_sent(session, outbox_id, 300)
        await session.commit()
        row = await session.get(VerdictOutbox, outbox_id)
        verdict = await session.get(Verdict, verdict_id)
        assert row.status == "sent" and row.delivered_message_id == 300
        assert verdict.message_id == 300
        assert await request_retry(session, post_id) is None
    await engine.dispose()


@pytest.mark.asyncio
async def test_expired_delivery_lease_is_reclaimed(tmp_path):
    engine, factory, outbox_id, _, _ = await make_outbox(tmp_path, "lease.db", 101)
    async with factory() as session:
        row = await session.get(VerdictOutbox, outbox_id)
        row.status = "delivering"
        row.attempts = 1
        row.lease_until = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()
        reclaimed = await claim_due(session)
        assert len(reclaimed) == 1 and reclaimed[0].attempts == 2
        await session.rollback()
    await engine.dispose()


@pytest.mark.asyncio
async def test_dispatcher_recovers_reply_sent_before_worker_crashed(tmp_path):
    engine, factory, outbox_id, _, verdict_id = await make_outbox(
        tmp_path,
        "recovery.db",
        500,
        {"title": "Ответ", "footer": {"text": "пост #500"}},
    )
    async with factory() as session:
        row = await session.get(VerdictOutbox, outbox_id)
        row.status = "delivering"
        row.attempts = 1
        row.lease_until = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()

    embed = discord.Embed(title="Ответ")
    embed.set_footer(text="пост #500")
    source = SimpleNamespace(id=500, reply_count=0)

    async def reply(**kwargs):
        source.reply_count += 1
        return SimpleNamespace(id=700)

    source.reply = reply
    existing = SimpleNamespace(
        id=700,
        author=SimpleNamespace(id=99),
        reference=SimpleNamespace(message_id=500),
        embeds=[embed],
    )
    await VPIBot.flush_outbox(fake_bot(factory, FakeChannel(source, [existing])))
    async with factory() as session:
        row = await session.get(VerdictOutbox, outbox_id)
        verdict = await session.get(Verdict, verdict_id)
        assert row.status == "sent" and row.delivered_message_id == 700
        assert verdict.message_id == 700 and source.reply_count == 0
    await engine.dispose()


@pytest.mark.asyncio
async def test_dispatcher_publishes_embed_as_a_reply_in_source_channel(tmp_path):
    engine, factory, outbox_id, _, verdict_id = await make_outbox(
        tmp_path,
        "reply.db",
        800,
        {"title": "Ответ", "footer": {"text": "время · пост #800"}},
    )
    source = SimpleNamespace(id=800, reply_call=None)

    async def reply(**kwargs):
        source.reply_call = kwargs
        return SimpleNamespace(id=901)

    source.reply = reply
    await VPIBot.flush_outbox(fake_bot(factory, FakeChannel(source)))
    assert source.reply_call is not None
    assert source.reply_call["mention_author"] is False
    assert source.reply_call["embed"].title == "Ответ"
    allowed = source.reply_call["allowed_mentions"]
    assert (
        not allowed.everyone
        and not allowed.users
        and not allowed.roles
        and not allowed.replied_user
    )
    async with factory() as session:
        row = await session.get(VerdictOutbox, outbox_id)
        verdict = await session.get(Verdict, verdict_id)
        assert row.status == "sent" and row.delivered_message_id == 901
        assert verdict.message_id == 901
    await engine.dispose()


@pytest.mark.asyncio
async def test_dispatcher_persists_failed_send_and_retry_time(tmp_path):
    engine, factory, outbox_id, _, verdict_id = await make_outbox(tmp_path, "failed-send.db", 950)
    source = SimpleNamespace(id=950)

    async def reply(**kwargs):
        raise RuntimeError("temporary Discord send failure")

    source.reply = reply
    await VPIBot.flush_outbox(fake_bot(factory, FakeChannel(source)))
    async with factory() as session:
        row = await session.get(VerdictOutbox, outbox_id)
        verdict = await session.get(Verdict, verdict_id)
        assert row.status == "pending" and row.attempts == 1
        assert row.lease_until is None
        assert "temporary Discord send failure" in row.last_error
        next_attempt = row.next_attempt_at
        if next_attempt.tzinfo is None:
            next_attempt = next_attempt.replace(tzinfo=UTC)
        assert next_attempt > datetime.now(UTC)
        assert verdict.message_id is None
    await engine.dispose()


@pytest.mark.asyncio
async def test_permanent_delivery_error_moves_to_dead_letter_until_admin_retry(tmp_path):
    engine, factory, outbox_id, post_id, _ = await make_outbox(tmp_path, "dead-letter.db", 980)
    async with factory() as session:
        assert len(await claim_due(session)) == 1
        await session.commit()
        await mark_failed(session, outbox_id, "source message was deleted", permanent=True)
        await session.commit()
        row = await session.get(VerdictOutbox, outbox_id)
        assert row.status == "dead" and row.last_error == "source message was deleted"
        assert await claim_due(session) == []
        await session.commit()
        assert await request_retry(session, post_id) == outbox_id
        await session.commit()
        assert len(await claim_due(session)) == 1
        await session.rollback()
    await engine.dispose()


@pytest.mark.parametrize(
    ("error", "permanent"),
    [
        (discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "missing"), True),
        (discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "denied"), True),
        (discord.HTTPException(SimpleNamespace(status=408, reason="Timeout"), "retry"), False),
        (discord.HTTPException(SimpleNamespace(status=425, reason="Too Early"), "retry"), False),
        (
            discord.HTTPException(SimpleNamespace(status=429, reason="Rate Limited"), "slow down"),
            False,
        ),
        (RuntimeError("temporary transport issue"), False),
    ],
)
def test_permanent_error_classifier_preserves_transient_retries(error, permanent):
    assert is_permanent_outbox_error(error) is permanent
