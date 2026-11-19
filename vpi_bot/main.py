import logging

from vpi_bot.bot import VPIBot
from vpi_bot.commands import install_commands
from vpi_bot.config import get_settings
from vpi_bot.database import SessionFactory


def main() -> None:
    settings = get_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if not settings.discord_token:
        raise SystemExit(
            "Не задан DISCORD_TOKEN. Скопируйте .env.example в .env и заполните токен."
        )
    bot = VPIBot(settings, SessionFactory)
    install_commands(bot)
    bot.run(settings.discord_token, log_handler=None)


if __name__ == "__main__":
    main()
