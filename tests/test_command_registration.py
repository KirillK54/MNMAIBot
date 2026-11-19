from types import SimpleNamespace

import discord
import pytest
from discord.ext import commands

from vpi_bot.bot import VPIBot
from vpi_bot.commands import install_commands
from vpi_bot.config import Settings


def test_install_commands_registers_all_slash_commands():
    bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())

    install_commands(bot)

    assert {command.name for command in bot.tree.get_commands()} == {
        "setup",
        "register",
        "panel",
        "verdict-mode",
        "game-rules",
        "post-limit",
        "admin",
        "countries",
        "country-view",
        "verdict-review",
        "verdict-approve",
        "verdict-reject",
        "post-retry",
        "outbox-status",
        "resource-catalog",
        "resource-add",
        "resource-adjust",
        "resource-disable",
        "resource-enable",
        "production-set",
        "epoch-set",
        "epoch-menu",
        "lore-add",
        "lore-edit",
        "lore-delete",
        "event-add",
        "lore-summary-set",
        "history-search",
        "industry-set",
        "country-delete",
        "country-multiple",
        "country-transfer",
        "country-edit",
        "military-set",
        "military-adjust",
        "technology-set",
        "post-grant",
        "exchange-rate-set",
        "exchange-rate-delete",
        "trade-offer",
        "trade-accept",
        "trade-decline",
        "economy-adjust",
        "verdict-amend",
        "verdict-cancel",
    }


def test_admin_commands_have_command_level_permission_checks():
    bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
    install_commands(bot)
    admin_commands = {
        "setup",
        "verdict-mode",
        "game-rules",
        "post-limit",
        "admin",
        "countries",
        "country-view",
        "country-multiple",
        "verdict-review",
        "verdict-approve",
        "verdict-reject",
        "post-retry",
        "outbox-status",
        "post-grant",
        "economy-adjust",
        "verdict-amend",
        "verdict-cancel",
        "resource-catalog",
        "resource-add",
        "resource-adjust",
        "resource-disable",
        "resource-enable",
        "production-set",
        "epoch-set",
        "epoch-menu",
        "lore-add",
        "lore-edit",
        "lore-delete",
        "event-add",
        "lore-summary-set",
        "history-search",
        "industry-set",
        "country-delete",
        "country-transfer",
        "country-edit",
        "military-set",
        "military-adjust",
        "technology-set",
        "exchange-rate-set",
        "exchange-rate-delete",
    }
    for name in admin_commands:
        assert bot.tree.get_command(name).checks, name


def test_every_slash_parameter_has_a_helpful_description():
    bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
    install_commands(bot)
    for command in bot.tree.get_commands():
        for option in command.to_dict(bot.tree).get("options", []):
            assert option.get("description") not in {None, "…", "..."}, (
                command.name,
                option["name"],
            )
            assert len(option["description"]) <= 100


def test_admin_autocomplete_callbacks_run_their_own_permission_checks():
    bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
    install_commands(bot)
    admin_fields = {
        "country-view": {"country_id"},
        "country-delete": {"country_id"},
        "country-transfer": {"country_id"},
        "country-edit": {"country_id"},
        "epoch-set": {"name"},
        "lore-add": {"country_id"},
        "industry-set": {"country_id"},
        "military-adjust": {"country_id"},
        "military-set": {"country_id"},
        "technology-set": {"country_id"},
        "resource-adjust": {"country_id", "resource_name"},
        "resource-disable": {"resource_name"},
        "resource-enable": {"resource_name"},
        "production-set": {"country_id", "resource_name"},
        "lore-edit": {"lore_id"},
        "lore-delete": {"lore_id"},
        "event-add": {"country_id"},
        "lore-summary-set": {"country_id"},
        "verdict-approve": {"verdict_id"},
        "verdict-reject": {"verdict_id"},
        "post-retry": {"discord_message_id"},
        "economy-adjust": {"country_id"},
        "verdict-amend": {"verdict_id"},
        "verdict-cancel": {"verdict_id"},
    }
    for command_name, parameter_names in admin_fields.items():
        command = bot.tree.get_command(command_name)
        for parameter_name in parameter_names:
            callback = command._params[parameter_name].autocomplete
            assert callback and callback.__discord_app_commands_checks__, (
                command_name,
                parameter_name,
            )

    assert not bot.tree.get_command("epoch-set")._params["name"].required
    assert not bot.tree.get_command("epoch-menu").parameters
    assert not getattr(
        bot.tree.get_command("panel")._params["country_id"].autocomplete,
        "__discord_app_commands_checks__",
        [],
    )
    assert not getattr(
        bot.tree.get_command("trade-offer")._params["recipient_country_id"].autocomplete,
        "__discord_app_commands_checks__",
        [],
    )


def test_bot_does_not_override_discord_py_interaction_dispatch():
    assert "on_interaction" not in VPIBot.__dict__


@pytest.mark.asyncio
async def test_verdict_prefix_command_submits_inline_and_reply_posts():
    bot = VPIBot(Settings(ai_api_key="test-key"), session_factory=object())
    submissions = []

    async def process(message, body):
        submissions.append((message, body))

    bot.process_game_post = process
    replies = []

    async def reply(content, **kwargs):
        replies.append(content)

    inline_message = object()
    inline_ctx = SimpleNamespace(
        guild=object(), author=SimpleNamespace(id=4), message=inline_message, reply=reply
    )
    await bot._submit_verdict_command(inline_ctx, "  Пост решения  ")

    source = SimpleNamespace(
        author=SimpleNamespace(id=4),
        channel=SimpleNamespace(id=9),
        content="Решение в отдельном сообщении",
    )

    async def fetch_message(message_id):
        assert message_id == 77
        return source

    reply_ctx = SimpleNamespace(
        guild=object(),
        author=SimpleNamespace(id=4),
        message=SimpleNamespace(reference=SimpleNamespace(message_id=77, resolved=None)),
        channel=SimpleNamespace(id=9, fetch_message=fetch_message),
        reply=reply,
    )
    await bot._submit_verdict_command(reply_ctx)
    assert submissions == [
        (inline_message, "Пост решения"),
        (source, "Решение в отдельном сообщении"),
    ]
    assert replies == []


@pytest.mark.asyncio
async def test_verdict_prefix_command_is_registered_and_rejects_posts_over_2000_chars():
    bot = VPIBot(Settings(ai_api_key="test-key"), session_factory=object())
    assert bot.get_command("verdict") is not None
    messages = []

    async def reply(content, **kwargs):
        messages.append(content)

    ctx = SimpleNamespace(guild=object(), message=object(), reply=reply)
    await bot._submit_verdict_command(ctx, "п" * 2001)
    assert messages and "2000 символов" in messages[0]
