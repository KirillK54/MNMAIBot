import pytest
from pydantic import ValidationError

from vpi_bot.config import Settings


def test_settings_validate_runtime_defaults():
    settings = Settings(
        game_timezone="Europe/Saratov",
        verdict_mode="moderation",
        global_daily_post_limit=0,
        resource_tick_hours=6,
    )
    assert settings.game_timezone == "Europe/Saratov"
    assert settings.verdict_mode == "moderation"
    assert settings.global_daily_post_limit == 0
    assert settings.resource_tick_hours == 6


def test_settings_reject_invalid_policy_values():
    with pytest.raises(ValidationError):
        Settings(verdict_mode="skip-validation")
    with pytest.raises(ValidationError):
        Settings(game_timezone="not-a-zone")
