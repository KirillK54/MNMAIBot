# Техническая документация

## Структура

```text
vpi_bot/
  ai/             AIProvider, JSON Schema, системный промпт
  ui/             Embed и Discord View
  bot.py          discord.py event handlers и slash-команды
  config.py       настройки из окружения
  database.py     async SQLAlchemy engine/session factory
  game_engine.py  валидация/применение/отмена дельт
  judge.py        сбор актуального контекста и запись вердикта
  models.py       таблицы и связи
  registration.py Modal регистрации государства
  security.py     проверка Guild привязки
migrations/       Alembic окружение и начальная ревизия
 tests/           автоматические проверки игровых инвариантов
```

## Провайдер ИИ

Контракт `AIProvider.judge(system_prompt, context, post) -> JudgeResult`. Реализуйте протокол для нового
API и инъецируйте провайдер в `VPIBot`; слой бота не должен зависеть от специфики SDK. Выход Pydantic
имеет поля результата, последствий, времени и двух отдельных словарей дельт: `changes.metrics` и
`changes.resources`.

## База данных

Модели определены в `vpi_bot.models`. Первичная ревизия `0001_initial` создаёт metadata. Меняйте схему
новой Alembic-ревизией и не полагайтесь на `create_all` в производстве. Экономические изменения
фиксируются как `StatChange` с дельтами для аудита и потенциальной отмены. Миграции `asyncpg` исполняются
через async Alembic env.

## Ограничения API и обработка ошибок

Discord Message Content Intent обязателен. Модель вызывается с timeout, провайдерские HTTP ошибки
логируются внешним обработчиком сообщения. Уникальность `discord_message_id` защищает от дублирующей
доставки gateway-события. Ответ отправляется `message.reply`, сохраняется его Discord ID.
Для Production следует дополнить очередью задач/outbox, backoff/retry AI, централизованной телеметрией,
политикой retention/backup и параллельными тестами PostgreSQL.

## Обновление

Остановите процесс, сделайте бэкап БД, поставьте новую версию, запустите `alembic upgrade head`,
затем службу. Проверьте `/panel` и публикацию тестового ответа в изолированном канале. Подробности
настройки — `docs/ADMIN_GUIDE.md`.
