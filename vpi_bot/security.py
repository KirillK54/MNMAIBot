import discord
from discord import app_commands
from sqlalchemy.ext.asyncio import AsyncSession

from vpi_bot.config import Settings
from vpi_bot.models import GuildConfig


async def authorized_guild(
    session: AsyncSession, guild_id: int, settings: Settings
) -> GuildConfig | None:
    if settings.authorized_guild_id and settings.authorized_guild_id != guild_id:
        return None
    config = await session.get(GuildConfig, guild_id)
    if config is not None:
        return config
    # A GuildConfig is created only by the administrator-only /setup command.
    return None


def admin_check(bot, *, allow_moderator: bool = False):
    """Permit Discord administrators or the configured server staff roles."""

    async def predicate(interaction: discord.Interaction) -> bool:
        if interaction.guild_id is None:
            return False
        if interaction.permissions.administrator:
            return True
        config = await bot.guild_config(interaction.guild_id)
        if config is None:
            return False
        allowed = {config.admin_role_id} if config.admin_role_id else set()
        if allow_moderator and config.moderator_role_id:
            allowed.add(config.moderator_role_id)
        member_roles = getattr(interaction.user, "roles", ())
        return any(role.id in allowed for role in member_roles)

    return app_commands.check(predicate)
