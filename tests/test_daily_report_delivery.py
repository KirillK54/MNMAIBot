from types import SimpleNamespace

import discord
import pytest

from vpi_bot.bot import send_daily_report


@pytest.mark.asyncio
async def test_missing_daily_report_channel_access_logs_permissions_without_traceback(caplog):
    class ForbiddenChannel:
        async def send(self, **kwargs):
            response = SimpleNamespace(status=403, reason="Forbidden")
            raise discord.Forbidden(response, {"message": "Missing Access", "code": 50001})

    await send_daily_report(ForbiddenChannel(), 1234, 2, discord.Embed(title="Report"))
    assert "Missing Access" in caplog.text
    assert "1234" in caplog.text and "2" in caplog.text
    assert "View Channel, Send Messages and Embed Links" in caplog.text
    assert "Traceback" not in caplog.text
