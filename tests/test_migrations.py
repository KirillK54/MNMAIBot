from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from vpi_bot.config import get_settings


def test_upgrade_adds_new_columns_to_a_database_at_0001(tmp_path: Path, monkeypatch):
    database = tmp_path / "legacy.db"
    sync_url = f"sqlite:///{database.as_posix()}"
    async_url = f"sqlite+aiosqlite:///{database.as_posix()}"
    monkeypatch.setenv("DATABASE_URL", async_url)
    get_settings.cache_clear()

    engine = create_engine(sync_url)
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
        connection.execute(text("INSERT INTO alembic_version(version_num) VALUES ('0001_initial')"))
        connection.execute(text("CREATE TABLE guild_configs (guild_id BIGINT PRIMARY KEY)"))
        connection.execute(text("INSERT INTO guild_configs(guild_id) VALUES (101)"))
        connection.execute(
            text("""
            CREATE TABLE game_posts (
                id INTEGER PRIMARY KEY,
                guild_id BIGINT NOT NULL,
                discord_message_id BIGINT NOT NULL UNIQUE,
                channel_id BIGINT NOT NULL,
                author_id BIGINT NOT NULL,
                country_id INTEGER NOT NULL,
                body TEXT NOT NULL,
                local_date DATE NOT NULL,
                state VARCHAR(20) NOT NULL,
                created_at DATETIME NOT NULL
            )
        """)
        )
        connection.execute(
            text("""
            INSERT INTO game_posts
                (id, guild_id, discord_message_id, channel_id, author_id, country_id,
                 body, local_date, state, created_at)
            VALUES (1, 101, 9001, 200, 6, 1, 'post', '2026-01-01', 'judged', CURRENT_TIMESTAMP)
        """)
        )
        connection.execute(
            text("""
            CREATE TABLE trade_offers (
                id INTEGER PRIMARY KEY,
                guild_id BIGINT NOT NULL,
                from_country_id INTEGER NOT NULL,
                to_country_id INTEGER NOT NULL,
                money NUMERIC(20, 2) NOT NULL,
                resources JSON NOT NULL,
                status VARCHAR(20) NOT NULL,
                created_at DATETIME NOT NULL
            )
        """)
        )
        connection.execute(
            text("""
            INSERT INTO trade_offers
                (id, guild_id, from_country_id, to_country_id, money, resources, status, created_at)
            VALUES (1, 101, 1, 2, 5, '{}', 'pending', CURRENT_TIMESTAMP)
        """)
        )

    cfg = Config("alembic.ini")
    command.upgrade(cfg, "head")

    inspector = inspect(engine)
    assert "verdict_outbox" in inspector.get_table_names()
    assert "research_projects" in inspector.get_table_names()
    assert "currency_exchange_rates" in inspector.get_table_names()
    assert {"processing_started_at", "attempts"} <= {
        column["name"] for column in inspector.get_columns("game_posts")
    }
    assert {"money_received", "exchange_rate"} <= {
        column["name"] for column in inspector.get_columns("trade_offers")
    }
    guild_columns = {column["name"] for column in inspector.get_columns("guild_configs")}
    assert {"timezone", "allow_multiple_countries", "system_channel_id"} <= guild_columns
    epoch_columns = {column["name"] for column in inspector.get_columns("epochs")}
    assert {"rules", "starting_stats"} <= epoch_columns
    with engine.connect() as connection:
        assert (
            connection.scalar(text("SELECT timezone FROM guild_configs WHERE guild_id=101"))
            == "Europe/Saratov"
        )
        assert connection.scalar(
            text("SELECT allow_multiple_countries FROM guild_configs WHERE guild_id=101")
        ) in (0, False)
        assert connection.scalar(text("SELECT attempts FROM game_posts WHERE id=1")) == 0
        assert connection.scalar(text("SELECT money_received FROM trade_offers WHERE id=1")) == 0
    engine.dispose()
    get_settings.cache_clear()
