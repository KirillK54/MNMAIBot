from datetime import UTC, datetime

from vpi_bot.ai.schemas import JudgeResult
from vpi_bot.models import Country, EconomicStats, Epoch
from vpi_bot.ui.cards import country_overview_embed, verdict_embed


def test_verdict_card_is_short_and_includes_date_verdict_and_remaining_posts():
    country = Country(
        guild_id=1,
        owner_id=2,
        name="Тестландия",
        government="Республика",
        religion="Нет",
        currency="рубль",
    )
    result = JudgeResult(
        status="partial_success",
        title="Ремонт дорог",
        description="Ремонт начат в пределах доступного бюджета.",
        reasoning="Подробные внутренние рассуждения не должны показываться.",
        conditions=["Дополнительные условия не должны показываться."],
        positives=["Улучшится логистика"],
    )
    embed = verdict_embed(
        result,
        country,
        post_id=1234,
        stamp="18.11.2026 20:00 MSK",
        verdict_id=56,
        posts_remaining=2,
    )
    field_names = {field.name for field in embed.fields}
    footer = embed.footer.text
    assert "Дополнительные условия" not in field_names
    assert "Комментарий игрового мастера" not in field_names
    assert "Подробные внутренние рассуждения" not in str(embed.to_dict())
    assert "18.11.2026" in footer
    assert "вердикт #56" in footer
    assert "осталось постов: 2" in footer
    assert "пост #1234" in footer


def test_country_card_displays_current_era_and_era_specific_start_values():
    country = Country(
        guild_id=1,
        owner_id=2,
        name="Каменная община",
        government="Совет вождей",
        religion="Анимизм",
        currency="камень",
        founded_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    stats = EconomicStats(
        treasury=250,
        gdp=900,
        income=20,
        expenses=15,
        inflation=3,
        unemployment=5,
        stability=55,
        industry_level=0,
    )
    era = Epoch(
        name="Каменный век",
        start_year=-100000,
        end_year=-3500,
        description="Охота, собирательство и каменные орудия.",
        technologies=["огонь", "каменные орудия"],
    )
    embed = country_overview_embed(country, stats, epoch=era)
    era_field = next(field for field in embed.fields if field.name == "🕰️ Эпоха")
    assert "100000 до н. э." in era_field.value
    assert "каменные орудия" in era_field.value
    treasury_field = next(field for field in embed.fields if field.name == "💰 Казна")
    assert "250" in treasury_field.value
