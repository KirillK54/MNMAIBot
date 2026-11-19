from sqlalchemy import URL, make_url, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool


def _url(value: str | URL) -> URL:
    return value if isinstance(value, URL) else make_url(value)


async def ensure_database_exists(database_url: str | URL) -> bool:
    """Create a missing PostgreSQL database using the same credentials; return True if created.

    The PostgreSQL role must be able to connect to the maintenance database and have CREATEDB
    privilege. SQLite creates its file automatically, so no bootstrap connection is needed there.
    """
    target = _url(database_url)
    if target.get_backend_name() != "postgresql" or not target.database:
        return False
    if target.database == "postgres":
        return False

    maintenance = target.set(database="postgres")
    engine = create_async_engine(maintenance, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            connection = await connection.execution_options(isolation_level="AUTOCOMMIT")
            exists = await connection.scalar(
                text("SELECT 1 FROM pg_database WHERE datname = :database_name"),
                {"database_name": target.database},
            )
            if exists:
                return False
            identifier = engine.sync_engine.dialect.identifier_preparer.quote(target.database)
            try:
                await connection.exec_driver_sql(f"CREATE DATABASE {identifier}")
            except SQLAlchemyError:
                # Another process may have created the database after our existence check.
                async with engine.connect() as check_connection:
                    exists = await check_connection.scalar(
                        text("SELECT 1 FROM pg_database WHERE datname = :database_name"),
                        {"database_name": target.database},
                    )
                if exists:
                    return False
                raise
        return True
    finally:
        await engine.dispose()
