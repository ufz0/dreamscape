"""Secrets at rest: what is stored encrypted, what the API may show, and where the key lives."""
import os
import stat

import cipher
import db
from conftest import make_user


def use_key_file(tmp_path, monkeypatch):
    """Point the cipher at a test key file, so the real data/secret.key is not touched."""
    monkeypatch.delenv("APP_SECRET_KEY", raising=False)
    monkeypatch.setattr(cipher, "KEY_FILE", tmp_path / "secret.key")
    return tmp_path / "secret.key"


def test_the_stored_value_is_not_plaintext(tmp_path, monkeypatch):
    use_key_file(tmp_path, monkeypatch)
    stored = cipher.encrypt("sk-test-1234567890")
    assert stored != "sk-test-1234567890"
    assert "sk-test" not in stored
    assert cipher.decrypt(stored) == "sk-test-1234567890"
    # Empty stays empty, so "no key" is not an encrypted empty string.
    assert cipher.encrypt("") == ""
    assert cipher.decrypt("") == ""


def test_the_key_comes_from_the_environment_when_it_is_set(monkeypatch):
    monkeypatch.setenv("APP_SECRET_KEY", "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa=")
    value = cipher.encrypt("secret")
    from cryptography.fernet import Fernet
    assert Fernet(os.environ["APP_SECRET_KEY"]).decrypt(value.encode()).decode() == "secret"


def test_a_key_is_generated_once_with_mode_600(tmp_path, monkeypatch):
    key_file = use_key_file(tmp_path, monkeypatch)
    first = cipher.key()
    assert key_file.exists()
    assert stat.S_IMODE(key_file.stat().st_mode) == 0o600
    assert cipher.key() == first, "the same key on every call, so stored values keep working"


def test_what_the_api_may_show_about_a_secret(tmp_path, monkeypatch):
    use_key_file(tmp_path, monkeypatch)
    stored = cipher.encrypt("sk-test-1234567890")
    shown = cipher.shown(stored)
    assert shown["set"] is True and shown["hint"] == "7890"
    assert "sk-test" not in str(shown), "the value itself is never in what the API returns"
    assert cipher.shown("") == {"set": False, "hint": ""}


def test_a_value_written_before_encryption_exists_is_read_back(tmp_path, monkeypatch):
    """Old rows are not lost: a stored value that is not a Fernet token is returned as it was."""
    use_key_file(tmp_path, monkeypatch)
    assert cipher.decrypt("plaintext-from-before") == "plaintext-from-before"
    assert cipher.shown("plaintext-from-before") == {"set": True, "hint": "fore"}


def test_secrets_in_the_settings_table_are_not_read_through_the_api(conn, tmp_path, monkeypatch):
    """A connection row stores the encrypted form; reading it back needs the cipher."""
    use_key_file(tmp_path, monkeypatch)
    import connections

    created = connections.create(conn, {"username": "test"}, {"name": "Cipher", "preset": "custom",
                                           "base_url": "http://example.invalid/v1", "api_key": "abc12345"})
    row = conn.execute("SELECT * FROM connections WHERE id = ?", (created["id"],)).fetchone()
    assert "abc12345" not in row["api_key"]
    assert connections.public(conn, row)["api_key"] == {"set": True, "hint": "2345"}

    # The target is what the client needs, and it is only built where a request is made.
    target = connections.target(row)
    assert target["headers"]["Authorization"] == "Bearer abc12345"

    # An audit row names the change, never the value.
    entry = conn.execute("SELECT detail FROM audit WHERE action = 'connection_create'").fetchone()
    assert "abc12345" not in entry["detail"]
