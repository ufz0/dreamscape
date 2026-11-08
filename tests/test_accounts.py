"""Accounts: hashing, validation, sessions, login rate limiting, the last-admin rule, audit."""
from datetime import datetime, timedelta, timezone

import pytest

import accounts
import db
from conftest import PASSWORD, add_usage, make_user

NOW = datetime(2026, 11, 8, 11, 0, tzinfo=timezone.utc)


def iso(moment):
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


def limits_parse(value):
    return datetime.fromisoformat(value)


def test_passwords_are_hashed_and_never_stored_plain(conn):
    user = make_user(conn, "ada")
    row = conn.execute("SELECT password_hash FROM users WHERE id = ?", (user["id"],)).fetchone()
    assert row["password_hash"].startswith("$argon2")
    assert "a long enough password" not in row["password_hash"]
    assert accounts.verify_password(row["password_hash"], "a long enough password")
    assert not accounts.verify_password(row["password_hash"], "wrong password")
    assert "password_hash" not in accounts.get_user(conn, user["id"])


def test_validation(conn):
    assert accounts.validate_username("Bob") is None            # case-insensitive, stored lowercase
    assert accounts.validate_username("ab") is not None
    assert accounts.validate_username("a" * 33) is not None
    assert accounts.validate_username("bob bloggs") is not None
    assert accounts.validate_username("bob+") is not None
    assert accounts.validate_password("a long enough password") is None
    assert accounts.validate_password("short") is not None
    with pytest.raises(ValueError):
        accounts.create_user(conn, "bob", "short", actor="test")
    make_user(conn, "ada")
    with pytest.raises(ValueError):
        accounts.create_user(conn, "ada", PASSWORD, actor="test")   # taken


def test_usernames_are_case_insensitive_unique(conn):
    first = make_user(conn, "Ada")
    with pytest.raises(ValueError):
        accounts.create_user(conn, "ada", PASSWORD, actor="test")
    assert accounts.get_by_username(conn, "ADA")["id"] == first["id"]


def test_first_user_is_the_admin(conn):
    user = make_user(conn, "firstadmin", role="admin")
    assert user["role"] == "admin"
    assert accounts.list_users(conn)[0]["role"] == "admin"


def test_migration_moves_data_and_keeps_the_wiki_git_history(conn):
    import appdata
    import config

    legacy = appdata.legacy_paths()
    legacy.wiki.mkdir(parents=True, exist_ok=True)
    legacy.inbox.mkdir(parents=True, exist_ok=True)
    legacy.runs.mkdir(parents=True, exist_ok=True)
    (legacy.wiki / "old-page.md").write_text("# Old page\n", encoding="utf-8")
    (legacy.wiki / ".git").mkdir(exist_ok=True)          # the wiki folder carries its own repo
    (legacy.inbox / "old-transcript.jsonl").write_text("{}\n", encoding="utf-8")
    (legacy.conversations).write_text("[]", encoding="utf-8")

    report = appdata.migrate_legacy(999)
    assert report["moved"], "existing data should have been moved"
    target = appdata.paths_for(999)
    assert (target.wiki / "old-page.md").exists()
    assert (target.wiki / ".git").exists(), "the wiki git history must survive"
    assert (target.inbox / "old-transcript.jsonl").exists()
    assert not (legacy.wiki / "old-page.md").exists()

    backups = list(config.DATA_DIR.parent.glob("data-backup-*"))
    assert backups, "a backup copy of data/ should have been made"
    assert (backups[0] / "wiki" / "old-page.md").exists()


def test_setup_only_runs_once(conn):
    from fastapi.testclient import TestClient
    import app

    client = TestClient(app.app)
    assert client.get("/setup").status_code == 200
    # While no account exists, every page goes to /setup and every API call is 401.
    gate = client.get("/", follow_redirects=False)
    assert gate.status_code == 307 and gate.headers["location"] == "/setup"
    assert client.get("/api/conversations").status_code == 401

    accounts.create_user(conn, "setupuser", PASSWORD, role="admin", actor="first-run")
    conn.commit()
    assert client.get("/setup").status_code == 404
    after = client.get("/", follow_redirects=False)
    assert after.status_code == 307 and after.headers["location"] == "/login"


def test_sessions_expire_and_slide(conn):
    user = make_user(conn, "slider", role="admin")
    token = accounts.create_session(conn, user)
    conn.commit()
    row = conn.execute("SELECT expires FROM sessions WHERE token_hash = ?", (accounts.hash_token(token),)).fetchone()
    assert (limits_parse(row["expires"]) - datetime.now(timezone.utc)).days >= 29
    assert accounts.get_session(conn, token)["username"] == "slider"

    # A session near the end of its life is extended.
    near = datetime.now(timezone.utc) + timedelta(days=6)
    conn.execute("UPDATE sessions SET expires = ? WHERE token_hash = ?", (iso(near), accounts.hash_token(token)))
    conn.commit()
    account = accounts.get_session(conn, token)
    assert account is not None
    extended = limits_parse(conn.execute("SELECT expires FROM sessions WHERE token_hash = ?",
                                         (accounts.hash_token(token),)).fetchone()["expires"])
    assert (extended - datetime.now(timezone.utc)).days >= 29

    expired = datetime.now(timezone.utc) - timedelta(minutes=1)
    conn.execute("UPDATE sessions SET expires = ? WHERE token_hash = ?", (iso(expired), accounts.hash_token(token)))
    conn.commit()
    assert accounts.get_session(conn, token) is None


def test_password_change_and_disable_invalidate_sessions(conn):
    admin = make_user(conn, "admin1", role="admin")
    bob = make_user(conn, "bob")
    token = accounts.create_session(conn, bob)
    conn.commit()
    assert accounts.get_session(conn, token)

    # The acting session is kept, the user's others are dropped.
    accounts.change_password(conn, admin, bob["id"], "a long enough password", "a new long password")
    assert accounts.get_session(conn, token) is None

    token = accounts.create_session(conn, bob)
    conn.commit()
    accounts.set_status(conn, admin, bob["id"], "disabled")
    assert accounts.get_session(conn, token) is None
    assert accounts.get_user(conn, bob["id"])["status"] == "disabled"
    # A disabled user cannot log in even with the right password.
    assert accounts.get_session(conn, token) is None


def test_reset_password_forces_a_change(conn):
    admin = make_user(conn, "admin2", role="admin")
    bob = make_user(conn, "bob2")
    accounts.reset_password(conn, admin, bob["id"], "a temporary long password")
    assert accounts.get_user(conn, bob["id"])["must_change_password"] is True
    row = conn.execute("SELECT password_hash FROM users WHERE id = ?", (bob["id"],)).fetchone()
    assert accounts.verify_password(row["password_hash"], "a temporary long password")
    assert any(row["action"] == "reset_password" for row in accounts.audit_log(conn))


def test_last_admin_is_protected(conn):
    admin = make_user(conn, "onlyadmin", role="admin")
    other = make_user(conn, "plainuser")
    for action in ("demote", "disable", "delete"):
        with pytest.raises(ValueError):
            if action == "demote":
                accounts.set_role(conn, admin, admin["id"], "user")
            elif action == "disable":
                accounts.set_status(conn, admin, admin["id"], "disabled")
            else:
                accounts.delete_user(conn, admin, admin["id"])

    # A second active admin makes the change possible, including self-demotion.
    second = make_user(conn, "secondadmin", role="admin")
    accounts.set_role(conn, admin, admin["id"], "user")
    assert accounts.get_user(conn, admin["id"])["role"] == "user"
    accounts.set_status(conn, second, admin["id"], "disabled")
    assert accounts.get_user(conn, admin["id"])["status"] == "disabled"
    # Now `second` is the last active admin and cannot be removed either.
    with pytest.raises(ValueError):
        accounts.delete_user(conn, second, second["id"])


def test_deleting_a_user_removes_their_folder_and_keeps_usage_anonymized(conn):
    import appdata
    admin = make_user(conn, "admin3", role="admin")
    bob = make_user(conn, "bob3")
    appdata.set_current(bob["id"])
    appdata.current().wiki.mkdir(parents=True, exist_ok=True)
    (appdata.current().wiki / "page.md").write_text("# Page\n", encoding="utf-8")
    add_usage(conn, bob["id"], 5000)
    result = accounts.delete_user(conn, admin, bob["id"])
    assert result["files_removed"] >= 1
    assert not appdata.user_dir(bob["id"]).exists()
    row = conn.execute("SELECT user_id, total_tokens FROM usage_events").fetchone()
    assert row["user_id"] is None and row["total_tokens"] == 5000
    assert any(entry["action"] == "delete" for entry in accounts.audit_log(conn))


def test_login_lockout_after_five_failures(conn):
    make_user(conn, "lockme", role="admin")
    assert accounts.lockout(conn, "lockme", "10.0.0.1") is None
    for _ in range(5):
        accounts.record_login(conn, "lockme", "10.0.0.1", False)
    blocked = accounts.lockout(conn, "lockme", "10.0.0.1")
    assert blocked and blocked["failures"] == 5
    # The same name from another IP is also blocked, and the same IP for another name.
    assert accounts.lockout(conn, "lockme", "10.0.0.2") is not None
    assert accounts.lockout(conn, "someone-else", "10.0.0.1") is not None
    assert accounts.lockout(conn, "someone-else", "10.0.0.2") is None

    conn.execute("DELETE FROM login_attempts")
    for index in range(5):
        conn.execute("INSERT INTO login_attempts (username, ip, at, ok) VALUES (?, ?, ?, 0)",
                     ("lockme", "10.0.0.1", iso(datetime.now(timezone.utc) - timedelta(minutes=11 + index))))
    conn.commit()
    # Five failures, but all older than the 10 minute window: login is allowed again.
    assert accounts.lockout(conn, "lockme", "10.0.0.1") is None
    accounts.record_login(conn, "lockme", "10.0.0.1", True)
    assert accounts.lockout(conn, "lockme", "10.0.0.1") is None


def test_self_registration_setting_is_off_by_default(conn):
    assert accounts.settings(conn) == {"allow_self_registration": False,
                                               "count_dreaming_toward_limits": False}
    admin = make_user(conn, "admin4", role="admin")
    settings = accounts.set_setting(conn, admin, "allow_self_registration", True)
    assert settings["allow_self_registration"] is True
    with pytest.raises(ValueError):
        accounts.set_setting(conn, admin, "unknown_key", True)


def limits_parse(value):
    return datetime.fromisoformat(value)
