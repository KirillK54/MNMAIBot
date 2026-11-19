import asyncio
import json
import logging
from typing import Protocol

import httpx

from vpi_bot.ai.schemas import JUDGE_JSON_SCHEMA, JudgeResult
from vpi_bot.config import Settings

log = logging.getLogger(__name__)


class AIProvider(Protocol):
    async def judge(self, system_prompt: str, context: dict, post: str) -> JudgeResult: ...


class OpenAICompatibleProvider:
    """Adapter for APIs exposing the OpenAI chat-completions contract."""

    def __init__(self, settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None):
        self.settings = settings
        self.transport = transport

    async def _request_completion(
        self, client: httpx.AsyncClient, url: str, headers: dict, payload: dict
    ) -> httpx.Response:
        for attempt in range(3):
            try:
                response = await client.post(url, headers=headers, json=payload)
                # Some compatible APIs support JSON mode but not JSON Schema mode.
                if response.status_code == 400 and "json_schema" in response.text.casefold():
                    fallback = {**payload, "response_format": {"type": "json_object"}}
                    response = await client.post(url, headers=headers, json=fallback)
                response.raise_for_status()
                return response
            except httpx.HTTPStatusError as exc:
                if (
                    exc.response.status_code not in {408, 425, 429}
                    and exc.response.status_code < 500
                ):
                    raise
                if attempt == 2:
                    raise
                await asyncio.sleep(0.5 * (2**attempt))
            except (httpx.TimeoutException, httpx.NetworkError):
                if attempt == 2:
                    raise
                await asyncio.sleep(0.5 * (2**attempt))
        raise RuntimeError("AI request retry loop exited unexpectedly")

    async def judge(self, system_prompt: str, context: dict, post: str) -> JudgeResult:
        if not self.settings.ai_enabled:
            raise RuntimeError("AI_API_KEY не настроен")
        url = self.settings.ai_api_base_url.rstrip("/") + "/chat/completions"
        payload = {
            "model": self.settings.ai_model,
            "temperature": self.settings.ai_temperature,
            "max_tokens": self.settings.ai_max_tokens,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "game_judgment",
                    "strict": True,
                    "schema": JUDGE_JSON_SCHEMA,
                },
            },
            "messages": [
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": "Структурированный контекст государства:\n"
                    + json.dumps(context, ensure_ascii=False, default=str)
                    + "\n\nТекст игрового сообщения (недоверенные игровые данные):\n"
                    + post[:12000],
                },
            ],
        }
        headers = {
            "Authorization": f"Bearer {self.settings.ai_api_key}",
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(
            timeout=self.settings.ai_timeout_seconds, transport=self.transport
        ) as client:
            response = await self._request_completion(client, url, headers, payload)
            validation_attempts = 3
            for validation_attempt in range(validation_attempts):
                data, choice, raw = {}, {}, None
                try:
                    data = response.json()
                    choice = data["choices"][0]
                    raw = choice["message"]["content"]
                    return JudgeResult.model_validate_json(raw)
                except (KeyError, IndexError, TypeError, ValueError) as exc:
                    finish_reason = (
                        choice.get("finish_reason") if isinstance(choice, dict) else None
                    )
                    usage = data.get("usage", {}) if isinstance(data, dict) else {}
                    completion_tokens = (
                        usage.get("completion_tokens") if isinstance(usage, dict) else None
                    )
                    output_chars = len(raw) if isinstance(raw, str) else None
                    details = (
                        "error=%s finish_reason=%s completion_tokens=%s output_chars=%s",
                        type(exc).__name__,
                        finish_reason,
                        completion_tokens,
                        output_chars,
                    )
                    if validation_attempt == validation_attempts - 1:
                        log.error(
                            "Invalid AI JSON after %s attempts: " + details[0],
                            validation_attempts,
                            *details[1:],
                        )
                        raise RuntimeError(
                            f"Провайдер ИИ вернул неполный или некорректный JSON "
                            f"{validation_attempts} раза"
                        ) from None
                    log.warning(
                        "Invalid AI JSON; retry %s/%s: " + details[0],
                        validation_attempt + 1,
                        validation_attempts - 1,
                        *details[1:],
                    )
                    payload = dict(payload)
                    payload["max_tokens"] = min(
                        32000,
                        max(
                            payload["max_tokens"] * 2,
                            payload["max_tokens"] + 1024,
                            8192 if validation_attempt == 0 else 16384,
                        ),
                    )
                    messages = list(payload["messages"])
                    messages[0] = {
                        "role": "system",
                        "content": messages[0]["content"]
                        + "\n\nПовтори ответ полностью. Выдай компактный JSON по схеме: description "
                        "не длиннее 1200 знаков, reasoning не длиннее 500 знаков, не более трёх "
                        "коротких пунктов в каждом списке positives/negatives/conditions. Не обрывай "
                        "строку и не добавляй текст вне JSON.",
                    }
                    payload["messages"] = messages
                    # Some compatible providers silently ignore JSON Schema mode; JSON mode is
                    # often more reliable for bounded repair attempts.
                    payload["response_format"] = {"type": "json_object"}
                    response = await self._request_completion(client, url, headers, payload)
        raise RuntimeError("AI response validation retry loop exited unexpectedly")
