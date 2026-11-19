from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def now_utc() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class GuildConfig(Base):
    __tablename__ = "guild_configs"
    guild_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    category_id: Mapped[int | None] = mapped_column(BigInteger)
    registered_role_id: Mapped[int | None] = mapped_column(BigInteger)
    admin_role_id: Mapped[int | None] = mapped_column(BigInteger)
    moderator_role_id: Mapped[int | None] = mapped_column(BigInteger)
    system_channel_id: Mapped[int | None] = mapped_column(BigInteger)
    reports_channel_id: Mapped[int | None] = mapped_column(BigInteger)
    allow_multiple_countries: Mapped[bool] = mapped_column(Boolean, default=False)
    timezone: Mapped[str] = mapped_column(String(64), default="Europe/Saratov")
    daily_post_limit: Mapped[int] = mapped_column(Integer, default=3)
    verdict_mode: Mapped[str] = mapped_column(String(16), default="automatic")
    game_rules: Mapped[str] = mapped_column(Text, default="")
    epoch_name: Mapped[str] = mapped_column(String(120), default="Индустриальная эпоха")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)


class Country(Base):
    __tablename__ = "countries"
    __table_args__ = (
        Index("ix_country_guild_owner_active", "guild_id", "owner_id", "is_active"),
        Index("ix_country_guild_channel", "guild_id", "channel_id"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, index=True)
    owner_id: Mapped[int] = mapped_column(BigInteger, index=True)
    name: Mapped[str] = mapped_column(String(100))
    government: Mapped[str] = mapped_column(String(40))
    religion: Mapped[str] = mapped_column(String(120))
    currency: Mapped[str] = mapped_column(String(80))
    channel_id: Mapped[int | None] = mapped_column(BigInteger, unique=True)
    info_message_id: Mapped[int | None] = mapped_column(BigInteger)
    flag_url: Mapped[str | None] = mapped_column(String(500))
    description: Mapped[str] = mapped_column(Text, default="")
    founded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    economy: Mapped[EconomicStats] = relationship(
        back_populates="country", uselist=False, cascade="all, delete-orphan"
    )
    inventories: Mapped[list[CountryResource]] = relationship(
        back_populates="country", cascade="all, delete-orphan"
    )


class EconomicStats(Base):
    __tablename__ = "economic_stats"
    country_id: Mapped[int] = mapped_column(
        ForeignKey("countries.id", ondelete="CASCADE"), primary_key=True
    )
    treasury: Mapped[Decimal] = mapped_column(Numeric(20, 2), default=Decimal(1000000))
    gdp: Mapped[Decimal] = mapped_column(Numeric(20, 2), default=Decimal(5000000))
    income: Mapped[Decimal] = mapped_column(Numeric(20, 2), default=Decimal(100000))
    expenses: Mapped[Decimal] = mapped_column(Numeric(20, 2), default=Decimal(80000))
    inflation: Mapped[Decimal] = mapped_column(Numeric(7, 3), default=Decimal("4.000"))
    unemployment: Mapped[Decimal] = mapped_column(Numeric(7, 3), default=Decimal("8.000"))
    growth: Mapped[Decimal] = mapped_column(Numeric(7, 3), default=Decimal("2.000"))
    debt: Mapped[Decimal] = mapped_column(Numeric(20, 2), default=Decimal(0))
    tax_revenue: Mapped[Decimal] = mapped_column(Numeric(20, 2), default=Decimal(50000))
    industry_level: Mapped[int] = mapped_column(Integer, default=1)
    stability: Mapped[Decimal] = mapped_column(Numeric(7, 3), default=Decimal("70.000"))
    country: Mapped[Country] = relationship(back_populates="economy")


class ResourceDefinition(Base):
    __tablename__ = "resource_definitions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, index=True)
    name: Mapped[str] = mapped_column(String(100))
    unit: Mapped[str] = mapped_column(String(30), default="ед.")
    description: Mapped[str] = mapped_column(Text, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    __table_args__ = (UniqueConstraint("guild_id", "name", name="uq_resource_guild_name"),)


class CountryResource(Base):
    __tablename__ = "country_resources"
    country_id: Mapped[int] = mapped_column(
        ForeignKey("countries.id", ondelete="CASCADE"), primary_key=True
    )
    resource_id: Mapped[int] = mapped_column(
        ForeignKey("resource_definitions.id", ondelete="CASCADE"), primary_key=True
    )
    quantity: Mapped[Decimal] = mapped_column(Numeric(20, 3), default=Decimal(0))
    country: Mapped[Country] = relationship(back_populates="inventories")
    resource: Mapped[ResourceDefinition] = relationship()


class ResourceProduction(Base):
    __tablename__ = "resource_production"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    country_id: Mapped[int] = mapped_column(
        ForeignKey("countries.id", ondelete="CASCADE"), index=True
    )
    resource_id: Mapped[int] = mapped_column(
        ForeignKey("resource_definitions.id", ondelete="CASCADE")
    )
    amount_per_tick: Mapped[Decimal] = mapped_column(Numeric(20, 3))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    interval_hours: Mapped[int] = mapped_column(Integer, default=4)
    last_tick_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ResourceOperation(Base):
    __tablename__ = "resource_operations"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    country_id: Mapped[int] = mapped_column(
        ForeignKey("countries.id", ondelete="CASCADE"), index=True
    )
    resource_id: Mapped[int] = mapped_column(
        ForeignKey("resource_definitions.id", ondelete="CASCADE"), index=True
    )
    source: Mapped[str] = mapped_column(String(32))
    source_id: Mapped[str | None] = mapped_column(String(120))
    delta: Mapped[Decimal] = mapped_column(Numeric(20, 3))
    before_value: Mapped[Decimal] = mapped_column(Numeric(20, 3))
    after_value: Mapped[Decimal] = mapped_column(Numeric(20, 3))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)


class EconomicOperation(Base):
    __tablename__ = "economic_operations"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    country_id: Mapped[int] = mapped_column(
        ForeignKey("countries.id", ondelete="CASCADE"), index=True
    )
    category: Mapped[str] = mapped_column(String(40))
    source: Mapped[str] = mapped_column(String(32))
    source_id: Mapped[str | None] = mapped_column(String(120))
    delta: Mapped[Decimal] = mapped_column(Numeric(24, 2))
    before_value: Mapped[Decimal] = mapped_column(Numeric(24, 2))
    after_value: Mapped[Decimal] = mapped_column(Numeric(24, 2))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)


class Epoch(Base):
    __tablename__ = "epochs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, index=True)
    name: Mapped[str] = mapped_column(String(120))
    description: Mapped[str] = mapped_column(Text, default="")
    start_year: Mapped[int | None] = mapped_column(Integer)
    end_year: Mapped[int | None] = mapped_column(Integer)
    restrictions: Mapped[dict] = mapped_column(JSON, default=dict)
    technologies: Mapped[list] = mapped_column(JSON, default=list)


class CountryLore(Base):
    __tablename__ = "country_lore"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    country_id: Mapped[int] = mapped_column(
        ForeignKey("countries.id", ondelete="CASCADE"), index=True
    )
    category: Mapped[str] = mapped_column(String(80), default="general")
    title: Mapped[str] = mapped_column(String(200))
    content: Mapped[str] = mapped_column(Text)
    importance: Mapped[int] = mapped_column(Integer, default=5)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc
    )


class GamePost(Base):
    __tablename__ = "game_posts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, index=True)
    discord_message_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    channel_id: Mapped[int] = mapped_column(BigInteger, index=True)
    author_id: Mapped[int] = mapped_column(BigInteger, index=True)
    country_id: Mapped[int] = mapped_column(
        ForeignKey("countries.id", ondelete="CASCADE"), index=True
    )
    body: Mapped[str] = mapped_column(Text)
    local_date: Mapped[date] = mapped_column(Date, index=True)
    state: Mapped[str] = mapped_column(String(20), default="received")
    processing_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)


class Verdict(Base):
    __tablename__ = "verdicts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    post_id: Mapped[int] = mapped_column(
        ForeignKey("game_posts.id", ondelete="CASCADE"), unique=True
    )
    status: Mapped[str] = mapped_column(String(32))
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text)
    reasoning: Mapped[str] = mapped_column(Text, default="")
    consequences: Mapped[dict] = mapped_column(JSON, default=dict)
    proposed_changes: Mapped[dict] = mapped_column(JSON, default=dict)
    applied_changes: Mapped[dict] = mapped_column(JSON, default=dict)
    duration_turns: Mapped[int] = mapped_column(Integer, default=0)
    moderator_comment: Mapped[str | None] = mapped_column(Text)
    message_id: Mapped[int | None] = mapped_column(BigInteger)
    state: Mapped[str] = mapped_column(String(20), default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)


class VerdictOutbox(Base):
    __tablename__ = "verdict_outbox"
    __table_args__ = (
        UniqueConstraint("post_id", name="uq_verdict_outbox_post"),
        Index("ix_verdict_outbox_due", "status", "next_attempt_at"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    post_id: Mapped[int] = mapped_column(
        ForeignKey("game_posts.id", ondelete="CASCADE"), index=True
    )
    verdict_id: Mapped[int] = mapped_column(
        ForeignKey("verdicts.id", ondelete="CASCADE"), unique=True
    )
    guild_id: Mapped[int] = mapped_column(BigInteger, index=True)
    channel_id: Mapped[int] = mapped_column(BigInteger, index=True)
    source_message_id: Mapped[int] = mapped_column(BigInteger, index=True)
    embed_payload: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivered_message_id: Mapped[int | None] = mapped_column(BigInteger)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc
    )


class StatChange(Base):
    __tablename__ = "stat_changes"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    verdict_id: Mapped[int] = mapped_column(
        ForeignKey("verdicts.id", ondelete="CASCADE"), index=True
    )
    country_id: Mapped[int] = mapped_column(
        ForeignKey("countries.id", ondelete="CASCADE"), index=True
    )
    change_key: Mapped[str] = mapped_column(String(120))
    before_value: Mapped[Decimal] = mapped_column(Numeric(24, 3))
    delta: Mapped[Decimal] = mapped_column(Numeric(24, 3))


class PostLimitGrant(Base):
    __tablename__ = "post_limit_grants"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    local_date: Mapped[date] = mapped_column(Date, index=True)
    extra_posts: Mapped[int] = mapped_column(Integer, default=1)
    granted_by: Mapped[int] = mapped_column(BigInteger)


class IndustryFacility(Base):
    __tablename__ = "industry_facilities"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    country_id: Mapped[int] = mapped_column(
        ForeignKey("countries.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(120))
    facility_type: Mapped[str] = mapped_column(String(80))
    level: Mapped[int] = mapped_column(Integer, default=1)
    production_per_tick: Mapped[dict] = mapped_column(JSON, default=dict)
    input_per_tick: Mapped[dict] = mapped_column(JSON, default=dict)
    source_project_id: Mapped[int | None] = mapped_column(
        ForeignKey("construction_projects.id", ondelete="SET NULL"), unique=True
    )
    operating_cost: Mapped[Decimal] = mapped_column(Numeric(20, 2), default=Decimal(0))
    interval_hours: Mapped[int] = mapped_column(Integer, default=4)
    last_tick_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(20), default="active")


class ConstructionProject(Base):
    __tablename__ = "construction_projects"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_verdict_id: Mapped[int | None] = mapped_column(
        ForeignKey("verdicts.id", ondelete="SET NULL"), index=True
    )
    country_id: Mapped[int] = mapped_column(
        ForeignKey("countries.id", ondelete="CASCADE"), index=True
    )
    project_type: Mapped[str] = mapped_column(String(80))
    name: Mapped[str] = mapped_column(String(160))
    cost: Mapped[Decimal] = mapped_column(Numeric(20, 2), default=Decimal(0))
    resource_costs: Mapped[dict] = mapped_column(JSON, default=dict)
    production_per_tick: Mapped[dict] = mapped_column(JSON, default=dict)
    input_per_tick: Mapped[dict] = mapped_column(JSON, default=dict)
    operating_cost: Mapped[Decimal] = mapped_column(Numeric(20, 2), default=Decimal(0))
    interval_hours: Mapped[int] = mapped_column(Integer, default=4)
    required_technology: Mapped[str | None] = mapped_column(String(160))
    duration_turns: Mapped[int] = mapped_column(Integer, default=1)
    completed_turns: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(20), default="building")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)


class ResearchProject(Base):
    __tablename__ = "research_projects"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_verdict_id: Mapped[int | None] = mapped_column(
        ForeignKey("verdicts.id", ondelete="SET NULL"), index=True
    )
    country_id: Mapped[int] = mapped_column(
        ForeignKey("countries.id", ondelete="CASCADE"), index=True
    )
    technology_name: Mapped[str] = mapped_column(String(160))
    target_level: Mapped[int] = mapped_column(Integer, default=1)
    description: Mapped[str] = mapped_column(Text, default="")
    duration_turns: Mapped[int] = mapped_column(Integer, default=1)
    completed_turns: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(20), default="researching")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)


class CountryTechnology(Base):
    __tablename__ = "country_technologies"
    __table_args__ = (UniqueConstraint("country_id", "name", name="uq_country_technology_name"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    country_id: Mapped[int] = mapped_column(
        ForeignKey("countries.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(160))
    level: Mapped[int] = mapped_column(Integer, default=1)
    description: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(20), default="researched")


class MilitaryStats(Base):
    __tablename__ = "military_stats"
    country_id: Mapped[int] = mapped_column(
        ForeignKey("countries.id", ondelete="CASCADE"), primary_key=True
    )
    personnel: Mapped[int] = mapped_column(Integer, default=0)
    units: Mapped[dict] = mapped_column(JSON, default=dict)
    equipment: Mapped[dict] = mapped_column(JSON, default=dict)
    ammunition: Mapped[dict] = mapped_column(JSON, default=dict)
    budget: Mapped[Decimal] = mapped_column(Numeric(20, 2), default=Decimal(0))
    technology_level: Mapped[int] = mapped_column(Integer, default=0)
    readiness: Mapped[Decimal] = mapped_column(Numeric(6, 2), default=Decimal(50))
    supply: Mapped[Decimal] = mapped_column(Numeric(6, 2), default=Decimal(50))


class TradeOffer(Base):
    __tablename__ = "trade_offers"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, index=True)
    from_country_id: Mapped[int] = mapped_column(
        ForeignKey("countries.id", ondelete="CASCADE"), index=True
    )
    to_country_id: Mapped[int] = mapped_column(
        ForeignKey("countries.id", ondelete="CASCADE"), index=True
    )
    money: Mapped[Decimal] = mapped_column(Numeric(20, 2), default=Decimal(0))
    money_received: Mapped[Decimal] = mapped_column(Numeric(20, 2), default=Decimal(0))
    exchange_rate: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))
    resources: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (Index("ix_trade_offer_pending", "guild_id", "status"),)


class CurrencyExchangeRate(Base):
    __tablename__ = "currency_exchange_rates"
    __table_args__ = (
        UniqueConstraint("guild_id", "from_currency", "to_currency", name="uq_exchange_rate_pair"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, index=True)
    from_currency: Mapped[str] = mapped_column(String(80))
    to_currency: Mapped[str] = mapped_column(String(80))
    rate: Mapped[Decimal] = mapped_column(Numeric(20, 6))
    updated_by: Mapped[int] = mapped_column(BigInteger)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc
    )


class DailyRun(Base):
    __tablename__ = "daily_runs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, index=True)
    country_id: Mapped[int] = mapped_column(
        ForeignKey("countries.id", ondelete="CASCADE"), index=True
    )
    local_date: Mapped[date] = mapped_column(Date, index=True)
    process: Mapped[str] = mapped_column(String(40))
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    report: Mapped[dict] = mapped_column(JSON, default=dict)
    __table_args__ = (
        UniqueConstraint(
            "country_id", "local_date", "process", name="uq_daily_run_country_process"
        ),
    )


class HistoricalEvent(Base):
    __tablename__ = "historical_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    verdict_id: Mapped[int | None] = mapped_column(
        ForeignKey("verdicts.id", ondelete="SET NULL"), unique=True
    )
    country_id: Mapped[int] = mapped_column(
        ForeignKey("countries.id", ondelete="CASCADE"), index=True
    )
    title: Mapped[str] = mapped_column(String(200))
    content: Mapped[str] = mapped_column(Text)
    event_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    importance: Mapped[int] = mapped_column(Integer, default=5)
    tags: Mapped[list] = mapped_column(JSON, default=list)


class LoreSummary(Base):
    __tablename__ = "lore_summaries"
    __table_args__ = (UniqueConstraint("country_id", name="uq_lore_summary_country"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    country_id: Mapped[int] = mapped_column(
        ForeignKey("countries.id", ondelete="CASCADE"), index=True
    )
    through_event_id: Mapped[int | None] = mapped_column(
        ForeignKey("historical_events.id", ondelete="SET NULL")
    )
    content: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc
    )


class AuditLog(Base):
    __tablename__ = "audit_logs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, index=True)
    actor_id: Mapped[int] = mapped_column(BigInteger)
    action: Mapped[str] = mapped_column(String(120))
    target_type: Mapped[str] = mapped_column(String(80))
    target_id: Mapped[str] = mapped_column(String(120))
    old_value: Mapped[str | None] = mapped_column(Text)
    new_value: Mapped[str | None] = mapped_column(Text)
    reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
