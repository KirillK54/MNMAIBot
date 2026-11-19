"""Safely inspect, back up, and clear the bot's configured database."""

from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import sqlite3
import subprocess
import sys
from collections.abc import Sequence
from contextlib import closing
from datetime import datetime
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import create_async_engine

PRESERVED_TABLES = {"alembic_version"}


def _timestamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S-%f")


def _quote_sqlite_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _sqlite_path(url: URL) -> Path:
    database = url.database
    if not database or database == ":memory:" or database.startswith("file:"):
        raise ValueError("Очистка поддерживает только файловую SQLite-базу.")
    path = Path(database).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    return path.resolve()


def _sqlite_tables(connection: sqlite3.Connection) -> list[str]:
    rows = connection.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    return [name for (name,) in rows]


def inspect_sqlite(path: Path) -> tuple[str, dict[str, int]]:
    if not path.is_file():
        raise FileNotFoundError(f"Файл базы SQLite не найден: {path}")
    with closing(sqlite3.connect(path)) as connection:
        tables = _sqlite_tables(connection)
        counts = {
            table: int(
                connection.execute(
                    f"SELECT COUNT(*) FROM {_quote_sqlite_identifier(table)}"
                ).fetchone()[0]
            )
            for table in tables
            if table not in PRESERVED_TABLES
        }
    return path.name, counts


def backup_sqlite(path: Path, backup_directory: Path) -> Path:
    if not path.is_file():
        raise FileNotFoundError(f"Файл базы SQLite не найден: {path}")
    backup_directory.mkdir(parents=True, exist_ok=True)
    backup_path = backup_directory / f"{path.stem}-{_timestamp()}.sqlite3"
    with (
        closing(sqlite3.connect(path)) as source,
        closing(sqlite3.connect(backup_path)) as destination,
    ):
        source.backup(destination)
    return backup_path


def clear_sqlite(path: Path) -> dict[str, int]:
    """Delete application rows while preserving schema and Alembic version."""
    if not path.is_file():
        raise FileNotFoundError(f"Файл базы SQLite не найден: {path}")
    deleted = {}
    with closing(sqlite3.connect(path, timeout=15)) as connection:
        connection.execute("PRAGMA busy_timeout = 15000")
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("BEGIN IMMEDIATE")
        try:
            for table in _sqlite_tables(connection):
                if table in PRESERVED_TABLES:
                    continue
                quoted = _quote_sqlite_identifier(table)
                deleted[table] = int(
                    connection.execute(f"SELECT COUNT(*) FROM {quoted}").fetchone()[0]
                )
                connection.execute(f"DELETE FROM {quoted}")
            if "sqlite_sequence" in {
                name
                for (name,) in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }:
                connection.execute("DELETE FROM sqlite_sequence")
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.execute("PRAGMA foreign_keys = ON")
    return deleted


async def inspect_postgres(url: URL) -> tuple[str, dict[str, int]]:
    engine = create_async_engine(url.set(drivername="postgresql+asyncpg"), pool_pre_ping=True)
    try:
        async with engine.connect() as connection:
            database_name = await connection.scalar(text("SELECT current_database()"))
            result = await connection.execute(
                text(
                    "SELECT tablename FROM pg_tables "
                    "WHERE schemaname = 'public' AND tablename <> 'alembic_version' "
                    "ORDER BY tablename"
                )
            )
            table_names = [row[0] for row in result]
            quote = connection.dialect.identifier_preparer.quote
            counts = {}
            for table in table_names:
                count = await connection.scalar(
                    text(f"SELECT COUNT(*) FROM {quote('public')}.{quote(table)}")
                )
                counts[table] = int(count or 0)
        return str(database_name), counts
    finally:
        await engine.dispose()


async def clear_postgres(url: URL) -> dict[str, int]:
    engine = create_async_engine(url.set(drivername="postgresql+asyncpg"), pool_pre_ping=True)
    deleted = {}
    try:
        async with engine.begin() as connection:
            database_name = await connection.scalar(text("SELECT current_database()"))
            result = await connection.execute(
                text(
                    "SELECT tablename FROM pg_tables "
                    "WHERE schemaname = 'public' AND tablename <> 'alembic_version' "
                    "ORDER BY tablename"
                )
            )
            table_names = [row[0] for row in result]
            quote = connection.dialect.identifier_preparer.quote
            schema = quote("public")
            for table in table_names:
                identifier = f"{schema}.{quote(table)}"
                count = await connection.scalar(text(f"SELECT COUNT(*) FROM {identifier}"))
                deleted[table] = int(count or 0)
            for table in table_names:
                identifier = f"{schema}.{quote(table)}"
                await connection.exec_driver_sql(
                    f"TRUNCATE TABLE {identifier} RESTART IDENTITY CASCADE"
                )
        if not database_name:
            raise RuntimeError("Не удалось определить имя PostgreSQL-базы.")
        return deleted
    finally:
        await engine.dispose()


def backup_postgres(url: URL, backup_directory: Path) -> Path:
    pg_dump = shutil.which("pg_dump")
    if pg_dump is None:
        raise RuntimeError(
            "Не найден pg_dump. Установите клиент PostgreSQL и добавьте его в PATH; база не очищена."
        )
    if not url.database:
        raise ValueError("В DATABASE_URL не указано имя PostgreSQL-базы.")
    backup_directory.mkdir(parents=True, exist_ok=True)
    backup_path = backup_directory / f"{url.database}-{_timestamp()}.dump"
    command = [
        pg_dump,
        "--no-password",
        "--format=custom",
        "--no-owner",
        "--no-acl",
        "--file",
        str(backup_path),
    ]
    if url.host:
        command.extend(("--host", url.host))
    if url.port:
        command.extend(("--port", str(url.port)))
    if url.username:
        command.extend(("--username", url.username))
    command.extend(("--dbname", url.database))
    environment = os.environ.copy()
    if url.password:
        environment["PGPASSWORD"] = url.password
    for option in ("sslmode", "sslcert", "sslkey", "sslrootcert"):
        value = url.query.get(option)
        if value:
            environment[f"PG{option.upper()}"] = str(value)
    result = subprocess.run(command, capture_output=True, text=True, env=environment, check=False)
    if result.returncode:
        try:
            backup_path.unlink(missing_ok=True)
        except OSError:
            pass
        detail = result.stderr.strip() or f"pg_dump завершился с кодом {result.returncode}"
        if url.password:
            detail = detail.replace(url.password, "[скрыто]")
        raise RuntimeError(f"Резервная копия не создана; база не очищена. {detail}")
    return backup_path


def _print_inspection(backend: str, target: str, counts: dict[str, int]) -> None:
    print(f"Тип базы: {backend}")
    print(f"Целевая база (токен подтверждения): {target}")
    if not counts:
        print("Таблиц приложения для очистки не найдено.")
        return
    print("Будут очищены таблицы:")
    for table, count in counts.items():
        print(f"  {table}: {count} строк")
    print("Таблица alembic_version и схема сохраняются.")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Создать резервную копию и очистить данные игровой базы VPI-бота."
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Выполнить очистку после резервного копирования (без флага только просмотр).",
    )
    parser.add_argument(
        "--confirm-database",
        help="Точное имя базы/SQLite-файла; обязательно вместе с --yes.",
    )
    parser.add_argument(
        "--backup-dir",
        default="backups",
        help="Каталог для автоматической резервной копии (по умолчанию backups).",
    )
    args = parser.parse_args(argv)
    if args.yes and not args.confirm_database:
        parser.error("Для очистки обязательно укажите --confirm-database с точным именем базы.")

    backup_path: Path | None = None
    try:
        from vpi_bot.config import get_settings

        url = make_url(get_settings().database_url)
        backend = url.get_backend_name()
        sqlite_path = _sqlite_path(url) if backend == "sqlite" else None
        if backend == "sqlite":
            target, counts = inspect_sqlite(sqlite_path)
        elif backend == "postgresql":
            target, counts = asyncio.run(inspect_postgres(url))
        else:
            raise ValueError(f"Тип базы {backend!r} не поддержан; очистка не выполнена.")

        _print_inspection(backend, target, counts)
        if not args.yes:
            print(
                "Пробный запуск: изменений нет. Для очистки повторите команду с --yes и --confirm-database."
            )
            return 0
        if args.confirm_database != target:
            raise ValueError(
                f"Имя подтверждения не совпало: ожидалось {target!r}; база не очищена."
            )
        if not counts:
            print("Очистка не требуется.")
            return 0

        backup_directory = Path(args.backup_dir).expanduser().resolve()
        if backend == "sqlite":
            backup_path = backup_sqlite(sqlite_path, backup_directory)
        else:
            backup_path = backup_postgres(url, backup_directory)
        print(f"Резервная копия: {backup_path}")
        if backend == "sqlite":
            deleted = clear_sqlite(sqlite_path)
        else:
            deleted = asyncio.run(clear_postgres(url))
        print(f"Очищено таблиц: {len(deleted)}")
        print("Готово. Перезапустите бота и выполните /setup заново.")
        return 0
    except (
        ValueError,
        RuntimeError,
        OSError,
        sqlite3.Error,
        subprocess.SubprocessError,
        SQLAlchemyError,
    ) as exc:
        if backup_path is not None:
            print(f"Резервная копия сохранена: {backup_path}", file=sys.stderr)
        print(f"ОШИБКА: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
