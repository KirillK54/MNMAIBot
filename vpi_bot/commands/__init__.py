from vpi_bot.commands import core, countries, diplomacy, lore, moderation, resources, world


def install_commands(bot) -> None:
    """Register all application commands on a VPI bot instance."""
    core.register(bot)
    moderation.register(bot)
    resources.register(bot)
    countries.register(bot)
    world.register(bot)
    lore.register(bot)
    diplomacy.register(bot)


__all__ = ["install_commands"]
