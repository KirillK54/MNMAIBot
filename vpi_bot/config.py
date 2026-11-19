from functools import lru_cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", env_ignore_empty=True
    )

    discord_token: str = ""
    discord_application_id: int | None = None
    database_url: str = "sqlite+aiosqlite:///./vpi_bot.db"
    authorized_guild_id: int | None = None
    ai_api_base_url: str = "https://api.openai.com/v1"
    ai_api_key: str = ""
    ai_model: str = "gpt-4o-mini"
    ai_temperature: float = Field(default=0.25, ge=0, le=2)
    ai_max_tokens: int = Field(default=1800, ge=128, le=32000)
    ai_timeout_seconds: int = Field(default=45, ge=1, le=300)
    game_timezone: str = "Europe/Saratov"
    global_daily_post_limit: int = Field(default=3, ge=0, le=100)
    resource_tick_hours: int = Field(default=4, ge=1, le=720)
    verdict_mode: str = "automatic"
    log_level: str = "INFO"

    @field_validator("verdict_mode")
    @classmethod
    def valid_verdict_mode(cls, value: str) -> str:
        if value not in {"automatic", "moderation"}:
            raise ValueError("VERDICT_MODE должен быть automatic или moderation")
        return value

    @field_validator("game_timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("GAME_TIMEZONE должен быть часовым поясом IANA") from exc
        return value

    @property
    def ai_enabled(self) -> bool:
        return bool(self.ai_api_key.strip())


@lru_cache
def get_settings() -> Settings:
    return Settings()
