"""The migration runner: a fresh database, an existing one with data, and the backup.

Everything here works on a separate database file, so the test fixture's schema is left alone.
"""
import sqlite3

import migrations
import pytest


def open_database(path):
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    return connection


def tables(connection):
    rows = connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    return {row["name"] for row in rows}


def test_the_migration_files_are_numbered_and_in_order(tmp_path, monkeypatch):
    monkeypatch.setattr(migrations, "DB", tmp_path / "app.db")
    found = migrations.files()
    assert found, "there must be at least the baseline"
    numbers = [int(path.name[:3]) for path in found]
    assert numbers == sorted(numbers)
    assert all(path.name[:3].isdigit() for path in found)


def test_a_fresh_database_gets_the_schema_and_records_the_version(tmp_path, monkeypatch):
    monkeypatch.setattr(migrations, "DB", tmp_path / "app.db")
    migrations.applied.clear()

    connection = open_database(tmp_path / "app.db")
    result = migrations.apply(connection)
    assert result["backup"] is None, "nothing to copy while the file does not exist yet"
    assert "users" in tables(connection) and "connections" in tables(connection) and "runs" in tables(connection)
    assert migrations.version(connection) >= 4

    rows = connection.execute("SELECT version FROM schema_version ORDER BY version").fetchall()
    assert [row["version"] for row in rows] == sorted(int(path.name[:3]) for path in migrations.files())

    # A second call with nothing pending does nothing.
    assert migrations.apply(connection)["applied"] == []
    connection.close()


def test_an_existing_database_with_data_keeps_working(tmp_path, monkeypatch):
    """A database created before migrations existed: the baseline changes nothing, the rest adds."""
    monkeypatch.setattr(migrations, "DB", tmp_path / "app.db")
    migrations.applied.clear()

    connection = open_database(tmp_path / "app.db")
    # The schema as it was written from code, without the newer tables or columns.
    connection.execute("""CREATE TABLE users (
        id INTEGER PRIMARY KEY, username TEXT NOT NULL UNIQUE, password_hash TEXT NOT NULL,
        role TEXT NOT NULL DEFAULT 'user', status TEXT NOT NULL DEFAULT 'active',
        must_change_password INTEGER NOT NULL DEFAULT 0, created TEXT NOT NULL, last_login TEXT,
        daily_limit INTEGER NOT NULL DEFAULT 0, weekly_limit INTEGER NOT NULL DEFAULT 0)""")
    connection.execute("""CREATE TABLE audit (id INTEGER PRIMARY KEY, actor TEXT NOT NULL,
        action TEXT NOT NULL, target TEXT NOT NULL DEFAULT '', detail TEXT NOT NULL DEFAULT '',
        at TEXT NOT NULL)""")
    connection.execute("CREATE TABLE app_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    connection.execute("INSERT INTO users (username, password_hash, created, daily_limit) "
                       "VALUES ('keptuser', 'hash', '2026-01-01T00:00:00+00:00', 1000)")
    connection.execute("INSERT INTO app_settings (key, value) VALUES ('allow_self_registration', 'on')")
    connection.commit()

    result = migrations.apply(connection)
    assert result["backup"], "a database that already has tables is copied before anything is changed"
    assert (tmp_path / "backups").is_dir()
    assert result["backup"].startswith(str(tmp_path / "backups" / "app-"))

    kept = connection.execute("SELECT * FROM users WHERE username = 'keptuser'").fetchone()
    assert kept and kept["daily_limit"] == 1000, "existing data must keep working"
    assert connection.execute("SELECT value FROM app_settings WHERE key = 'allow_self_registration'").fetchone()["value"] == "on"
    assert "connections" in tables(connection) and "runs" in tables(connection)
    columns = {row[1] for row in connection.execute("PRAGMA table_info(usage_events)").fetchall()}
    assert "connection_id" in columns
    connection.close()


def test_each_file_is_one_transaction(tmp_path, monkeypatch):
    """A file that fails halfway leaves nothing applied, and the version table is not written."""
    monkeypatch.setattr(migrations, "DB", tmp_path / "app.db")
    migrations.applied.clear()

    folder = tmp_path / "migrations"
    folder.mkdir()
    (folder / "001_one.sql").write_text("CREATE TABLE one (id INTEGER PRIMARY KEY);\n"
                                        "INSERT INTO one (id) VALUES (1);", encoding="utf-8")
    (folder / "002_two.sql").write_text("CREATE TABLE two (id INTEGER PRIMARY KEY);\n"
                                        "INSERT INTO two_missing_table (id) VALUES (1);", encoding="utf-8")
    monkeypatch.setattr(migrations, "MIGRATIONS", folder)

    connection = open_database(tmp_path / "app.db")
    with pytest.raises(sqlite3.OperationalError):
        migrations.apply(connection)
    assert "one" in tables(connection), "the first file completed and committed"
    assert "two" not in tables(connection), "the half-run file is rolled back"
    assert migrations.version(connection) == 1, "only the completed file is recorded"
    connection.close()
