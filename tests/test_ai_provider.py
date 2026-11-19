import asyncio
import json

import httpx
import pytest

from vpi_bot.ai.provider import OpenAICompatibleProvider
from vpi_bot.config import Settings

VALID_CONTENT = json.dumps(
    {
        "is_game_action": True,
        "status": "partial_success",
        "title": "Ремонт дорог",
        "description": "Начаты работы в пределах бюджета.",
        "reasoning": "Есть материалы и рабочая сила.",
        "positives": ["Улучшится логистика"],
        "negatives": [],
        "conditions": [],
        "changes": {
            "metrics": {"treasury": -100},
            "resources": {"Железо": -2},
            "military": {"metrics": {}, "units": {}, "equipment": {}, "ammunition": {}},
            "projects": [],
            "research": [],
        },
        "duration_turns": 2,
    },
    ensure_ascii=False,
)


def response(content: str, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json={"choices": [{"message": {"content": content}}]})


@pytest.mark.asyncio
async def test_provider_sends_structured_request_and_parses_result():
    requests = []

    def handler(request: httpx.Request):
        payload = json.loads(request.content)
        requests.append(payload)
        return response(VALID_CONTENT)

    settings = Settings(ai_api_key="test-key", ai_api_base_url="https://example.test/v1")
    provider = OpenAICompatibleProvider(settings, transport=httpx.MockTransport(handler))
    result = await provider.judge("system rules", {"country": "A"}, "player post")
    assert result.title == "Ремонт дорог"
    assert result.changes.resources == {"Железо": -2.0}
    assert requests[0]["response_format"]["type"] == "json_schema"
    assert requests[0]["messages"][0]["content"] == "system rules"
    assert "player post" in requests[0]["messages"][1]["content"]


@pytest.mark.asyncio
async def test_provider_falls_back_to_json_mode_when_schema_is_unsupported():
    formats = []

    def handler(request: httpx.Request):
        payload = json.loads(request.content)
        formats.append(payload["response_format"]["type"])
        if len(formats) == 1:
            return httpx.Response(400, json={"error": "json_schema is not supported"})
        return response(VALID_CONTENT)

    provider = OpenAICompatibleProvider(
        Settings(ai_api_key="test-key"), transport=httpx.MockTransport(handler)
    )
    result = await provider.judge("rules", {}, "post")
    assert result.is_game_action
    assert formats == ["json_schema", "json_object"]


@pytest.mark.asyncio
async def test_provider_retries_rate_limits(monkeypatch):
    attempts = 0
    waits = []

    async def no_wait(seconds):
        waits.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", no_wait)

    def handler(request: httpx.Request):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(429, json={"error": "rate limited"})
        return response(VALID_CONTENT)

    provider = OpenAICompatibleProvider(
        Settings(ai_api_key="test-key"), transport=httpx.MockTransport(handler)
    )
    result = await provider.judge("rules", {}, "post")
    assert result.status == "partial_success"
    assert attempts == 2 and waits == [0.5]


@pytest.mark.asyncio
async def test_provider_retries_truncated_json_with_larger_budget():
    requests = []
    responses = ['{"is_game_action": true, "title": "Обрезанный ответ', VALID_CONTENT]

    def handler(request: httpx.Request):
        requests.append(json.loads(request.content))
        return response(responses[len(requests) - 1])

    provider = OpenAICompatibleProvider(
        Settings(ai_api_key="test-key", ai_max_tokens=512),
        transport=httpx.MockTransport(handler),
    )
    result = await provider.judge("system rules", {}, "player post")
    assert result.title == "Ремонт дорог"
    assert len(requests) == 2
    assert requests[1]["max_tokens"] > requests[0]["max_tokens"]
    assert requests[1]["response_format"] == {"type": "json_object"}
    assert "компактный JSON" in requests[1]["messages"][0]["content"]


@pytest.mark.asyncio
async def test_provider_uses_third_bounded_attempt_after_two_truncated_outputs():
    requests = []
    responses = [
        '{"status":"success","description":"cut off',
        '{"status":"success","description":"still cut off',
        VALID_CONTENT,
    ]

    def handler(request: httpx.Request):
        requests.append(json.loads(request.content))
        return response(responses[len(requests) - 1])

    provider = OpenAICompatibleProvider(
        Settings(ai_api_key="test-key", ai_max_tokens=512),
        transport=httpx.MockTransport(handler),
    )
    result = await provider.judge("system rules", {}, "player post")
    assert result.title == "Ремонт дорог"
    assert [request["max_tokens"] for request in requests] == [512, 8192, 16384]
    assert [request["response_format"]["type"] for request in requests] == [
        "json_schema",
        "json_object",
        "json_object",
    ]


@pytest.mark.asyncio
async def test_provider_does_not_log_or_rethrow_raw_invalid_output(caplog):
    private_marker = "untrusted-provider-output-marker"

    def handler(request: httpx.Request):
        return response(f'{{"description":"{private_marker}')

    provider = OpenAICompatibleProvider(
        Settings(ai_api_key="test-key"), transport=httpx.MockTransport(handler)
    )
    with pytest.raises(RuntimeError, match="некорректный JSON"):
        await provider.judge("system rules", {}, "player post")
    assert private_marker not in caplog.text
