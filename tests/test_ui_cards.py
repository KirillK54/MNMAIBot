from vpi_bot.ai.schemas import JudgeResult
from vpi_bot.models import Country
from vpi_bot.ui.cards import verdict_embed


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
