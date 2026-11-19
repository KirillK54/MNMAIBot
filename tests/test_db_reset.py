import sqlite3

from vpi_bot.config import get_settings
from vpi_bot.db_reset import backup_sqlite, clear_sqlite, inspect_sqlite, main


def create_database(path):
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            "CREATE TABLE countries (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE posts (id INTEGER PRIMARY KEY AUTOINCREMENT, country_id INTEGER "
            "REFERENCES countries(id), body TEXT NOT NULL)"
        )
        connection.execute("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)")
        connection.execute("INSERT INTO alembic_version VALUES ('0002_current')")
        connection.execute("INSERT INTO countries (name) VALUES ('Testland')")
        connection.execute("INSERT INTO posts (country_id, body) VALUES (1, 'test post')")


def test_sqlite_reset_clears_rows_resets_ids_and_preserves_schema_migration(tmp_path):
    database = tmp_path / "game.db"
    create_database(database)

    target, counts = inspect_sqlite(database)
    assert target == "game.db"
    assert counts == {"countries": 1, "posts": 1}

    deleted = clear_sqlite(database)
    assert deleted == counts
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM countries").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM posts").fetchone()[0] == 0
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone()[0] == (
            "0002_current"
        )
        connection.execute("INSERT INTO countries (name) VALUES ('Freshland')")
        assert connection.execute("SELECT id FROM countries").fetchone()[0] == 1


def test_sqlite_backup_is_a_readable_copy(tmp_path):
    database = tmp_path / "game.db"
    create_database(database)
    backup = backup_sqlite(database, tmp_path / "backups")

    with sqlite3.connect(backup) as connection:
        assert connection.execute("SELECT name FROM countries").fetchone()[0] == "Testland"
        assert connection.execute("SELECT body FROM posts").fetchone()[0] == "test post"


def test_reset_cli_defaults_to_dry_run_and_requires_exact_database_name(
    tmp_path, monkeypatch, capsys
):
    database = tmp_path / "safe.db"
    create_database(database)
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{database.as_posix()}")
    get_settings.cache_clear()
    backup_dir = tmp_path / "backups"

    assert main([]) == 0
    assert "Пробный запуск" in capsys.readouterr().out
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM countries").fetchone()[0] == 1

    assert main(["--yes", "--confirm-database", "wrong.db", "--backup-dir", str(backup_dir)]) == 1
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM countries").fetchone()[0] == 1
    capsys.readouterr()

    assert (
        main(["--yes", "--confirm-database", database.name, "--backup-dir", str(backup_dir)]) == 0
    )
    output = capsys.readouterr().out
    assert "Резервная копия" in output
    assert list(backup_dir.glob("safe-*.sqlite3"))
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM countries").fetchone()[0] == 0
    get_settings.cache_clear()
