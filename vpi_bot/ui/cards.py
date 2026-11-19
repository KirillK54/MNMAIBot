import discord

from vpi_bot.ai.schemas import JudgeResult
from vpi_bot.models import Country, EconomicStats, Epoch, MilitaryStats

COLORS = {
    "success": 0x2ECC71,
    "partial_success": 0xF1C40F,
    "failure": 0xE67E22,
    "catastrophic_failure": 0x992D22,
    "rejected": 0xE74C3C,
    "delayed": 0x3498DB,
    "conditions_required": 0x9B59B6,
}

METRIC_LABELS = {
    "treasury": "💰 Казна",
    "gdp": "📈 ВВП",
    "income": "⬆️ Доходы",
    "expenses": "⬇️ Расходы",
    "inflation": "🔥 Инфляция",
    "unemployment": "👥 Безработица",
    "growth": "🌱 Рост",
    "debt": "🏦 Долг",
    "stability": "🛡️ Стабильность",
    "industry_level": "🏭 Промышленность",
    "tax_revenue": "🧾 Налоговые доходы",
}

STATUS_LABELS = {
    "success": "✅ Успех",
    "partial_success": "🟡 Частичный успех",
    "failure": "🟠 Неудача",
    "catastrophic_failure": "🔴 Провал",
    "rejected": "⛔ Отклонено",
    "delayed": "⏳ Отложено",
    "conditions_required": "⚖️ Нужна проверка",
}


def verdict_embed(
    result: JudgeResult,
    country: Country,
    post_id: int,
    stamp: str,
    verdict_id: int | None = None,
    posts_remaining: int | None = None,
) -> discord.Embed:
    """Render a concise verdict card without internal commentary or extra conditions."""
    embed = discord.Embed(
        title=f"⚖️ {result.title[:200]}",
        description=(result.description.strip() or "Решение обработано.")[:1400],
        color=COLORS.get(result.status, 0x5865F2),
    )
    embed.add_field(name="🏛️ Государство", value=country.name[:100], inline=True)
    embed.add_field(
        name="📌 Итог",
        value=STATUS_LABELS.get(result.status, result.status.replace("_", " "))[:100],
        inline=True,
    )

    consequence_lines = [f"✅ {line[:180]}" for line in result.positives[:2]]
    consequence_lines.extend(f"⚠️ {line[:180]}" for line in result.negatives[:2])
    if consequence_lines:
        embed.add_field(name="Последствия", value="\n".join(consequence_lines)[:700], inline=False)

    changes = result.changes
    lines = [f"{METRIC_LABELS.get(key, key)}: {value:+g}" for key, value in changes.metrics.items()]
    lines.extend(f"📦 {key}: {value:+g}" for key, value in changes.resources.items())
    for group, label in (
        ("metrics", "показатель"),
        ("units", "подразделение"),
        ("equipment", "техника"),
        ("ammunition", "боеприпасы"),
    ):
        lines.extend(
            f"🛡️ {label} · {key}: {value:+g}"
            for key, value in getattr(changes.military, group).items()
        )
    lines.extend(
        f"🏗️ {project.name} — {project.duration_turns} ход(а)" for project in changes.projects
    )
    lines.extend(
        f"🔬 {research.technology_name} — {research.duration_turns} ход(а)"
        for research in changes.research
    )
    if lines:
        embed.add_field(name="Изменения", value="\n".join(lines[:10])[:1000], inline=False)

    footer = [stamp]
    if verdict_id is not None:
        footer.append(f"вердикт #{verdict_id}")
    if posts_remaining is not None:
        footer.append(f"осталось постов: {posts_remaining}")
    # This marker is used by the durable outbox to reconcile a reply sent before a crash.
    footer.append(f"пост #{post_id}")
    embed.set_footer(text=" · ".join(footer)[:2048])
    return embed


def country_overview_embed(
    country: Country,
    stats: EconomicStats | None = None,
    army: MilitaryStats | None = None,
    *,
    resource_count: int = 0,
    epoch: Epoch | None = None,
) -> discord.Embed:
    stability = float(stats.stability) if stats else 0
    color = 0x2ECC71 if stability >= 70 else 0xF1C40F if stability >= 40 else 0xE74C3C
    embed = discord.Embed(
        title=f"🏛️ {country.name}",
        description=(country.description or "Государственная карточка")[:900],
        color=color,
    )
    if country.flag_url:
        embed.set_thumbnail(url=country.flag_url)
    embed.add_field(name="👑 Правление", value=country.government, inline=True)
    embed.add_field(name="🛐 Религия", value=country.religion, inline=True)
    embed.add_field(name="💱 Валюта", value=country.currency, inline=True)
    if epoch:
        start = _era_year(epoch.start_year) if epoch.start_year is not None else "?"
        end = _era_year(epoch.end_year) if epoch.end_year is not None else "далее"
        technologies = ", ".join(epoch.technologies[:5]) or "уточняет администратор"
        era_text = f"{start} — {end}"
        if epoch.description:
            era_text += f"\n{epoch.description[:300]}"
        era_text += f"\nВозможные технологии: {technologies}"[:500]
        embed.add_field(name="🕰️ Эпоха", value=era_text[:1000], inline=False)
    if stats:
        embed.add_field(
            name="💰 Казна", value=f"{stats.treasury:,.2f} {country.currency}", inline=True
        )
        embed.add_field(name="📈 ВВП", value=f"{stats.gdp:,.2f} {country.currency}", inline=True)
        embed.add_field(
            name="🧾 Доход / расход",
            value=f"{stats.income:,.0f} / {stats.expenses:,.0f}",
            inline=True,
        )
        embed.add_field(
            name="🛡️ Стабильность",
            value=f"{_meter(stability)} {stability:.0f}%",
            inline=True,
        )
        embed.add_field(
            name="📊 Инфляция / безработица",
            value=f"{stats.inflation:.1f}% / {stats.unemployment:.1f}%",
            inline=True,
        )
        embed.add_field(name="🏭 Промышленность", value=f"ур. {stats.industry_level}", inline=True)
    if army:
        embed.add_field(
            name="🪖 Армия",
            value=f"{army.personnel:,} человек · готовность {army.readiness:.0f}%",
            inline=True,
        )
    embed.add_field(name="📦 Активных ресурсов", value=str(resource_count), inline=True)
    embed.set_footer(text=f"Государство #{country.id} · основано {country.founded_at:%d.%m.%Y}")
    return embed


def _meter(value: float) -> str:
    filled = max(0, min(10, round(value / 10)))
    return "🟩" * filled + "⬜" * (10 - filled)


def _era_year(year: int) -> str:
    return f"{abs(year)} до н. э." if year < 0 else f"{year} н. э."
