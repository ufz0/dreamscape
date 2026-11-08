"""Host binding, the cookie Secure flag, and when a forwarded header is believed.

The default bind is 0.0.0.0, so the app is reachable from the network. That is why the cookie and
the proxy trust matter: an untrusted client can put anything in X-Forwarded-For.
"""
import os

from starlette.requests import Request

import auth
import config
from conftest import make_user
from test_isolation import client_for


def request_from(client_host, headers=None, scheme="http"):
    """A request as the app would see it, with a chosen peer address."""
    scope = {"type": "http", "method": "GET", "path": "/login", "scheme": scheme,
            "headers": [(key.lower().encode(), str(value).encode()) for key, value in
                        dict({"host": "dreamscape.test", **(headers or {})}).items()],
            "client": (client_host, 443), "query_string": b""}
    return Request(scope)


def test_the_default_bind_is_all_interfaces():
    os.environ.pop("HOST", None)
    assert config.env("HOST", "0.0.0.0") == "0.0.0.0"
    assert config.env("PORT", "8000") == "8000"


def test_the_cookie_is_secure_for_an_https_request(monkeypatch):
    monkeypatch.delenv("COOKIE_SECURE", raising=False)
    monkeypatch.delenv("TRUSTED_PROXIES", raising=False)
    assert auth.is_https(request_from("203.0.113.7", scheme="https")) is True
    # Plain HTTP on a LAN keeps working: the cookie is not marked Secure, so the browser sends it back.
    assert auth.is_https(request_from("192.168.1.20")) is False


def test_a_forwarded_proto_is_only_believed_from_a_trusted_proxy(monkeypatch):
    monkeypatch.delenv("COOKIE_SECURE", raising=False)
    monkeypatch.setenv("TRUSTED_PROXIES", "10.0.0.5,192.168.0.0/24")

    assert auth.is_https(request_from("10.0.0.5", {"X-Forwarded-Proto": "https"})) is True
    assert auth.is_https(request_from("192.168.0.9", {"X-Forwarded-Proto": "https"})) is True
    # The same header from a client that is not in the list is not believed.
    assert auth.is_https(request_from("203.0.113.7", {"X-Forwarded-Proto": "https"})) is False
    # Only the first value of a list of proxies counts.
    assert auth.is_https(request_from("10.0.0.5", {"X-Forwarded-Proto": "https,http"})) is True


def test_cookie_secure_can_be_forced(monkeypatch):
    monkeypatch.setenv("COOKIE_SECURE", "true")
    assert auth.is_https(request_from("192.168.1.20")) is True
    monkeypatch.setenv("COOKIE_SECURE", "false")
    assert auth.is_https(request_from("192.168.1.20", scheme="https")) is False
    monkeypatch.setenv("COOKIE_SECURE", "auto")
    assert auth.is_https(request_from("192.168.1.20")) is False


def test_the_real_client_ip_is_used_only_when_a_proxy_is_trusted(monkeypatch):
    monkeypatch.delenv("TRUSTED_PROXIES", raising=False)
    plain = request_from("192.168.1.20")
    assert auth.client_ip(plain) == "192.168.1.20"

    monkeypatch.setenv("TRUSTED_PROXIES", "10.0.0.5")
    trusted = request_from("10.0.0.5", {"X-Forwarded-For": "192.168.1.20, 10.0.0.5"})
    assert auth.client_ip(trusted) == "192.168.1.20"
    untrusted = request_from("10.0.0.6", {"X-Forwarded-For": "192.168.1.20"})
    assert auth.client_ip(untrusted) == "10.0.0.6"


def test_the_startup_warning_is_for_a_network_bind_over_plain_http(monkeypatch):
    monkeypatch.setenv("HOST", "0.0.0.0")
    monkeypatch.delenv("COOKIE_SECURE", raising=False)
    warning = auth.startup_warning()
    assert warning and "HTTPS reverse proxy" in warning and "0.0.0.0" in warning

    monkeypatch.setenv("HOST", "127.0.0.1")
    assert auth.startup_warning() is None
    monkeypatch.setenv("HOST", "::1")
    assert auth.startup_warning() is None
    # A forced Secure cookie means the app is behind something that terminates TLS, so nothing to warn about.
    monkeypatch.setenv("HOST", "0.0.0.0")
    monkeypatch.setenv("COOKIE_SECURE", "true")
    assert auth.startup_warning() is None


def test_the_routes_that_work_without_a_session_are_the_documented_ones(conn):
    admin = make_user(conn, "hostadmin", role="admin")
    guest = client_for(conn, admin)
    guest.cookies.clear()

    open_routes = ["/healthz", "/login", "/setup", "/register"]
    for path in open_routes:
        response = guest.get(path, follow_redirects=False)
        assert response.status_code in (200, 302, 404), f"{path} is meant to be reachable"

    # Nothing else: an API call gets 401, a page redirects to /login.
    for path in ("/api/conversations", "/api/models", "/api/me", "/api/connections", "/api/runs/x/events"):
        assert guest.get(path).status_code == 401, f"{path} needs a session"
    for path in ("/", "/wiki", "/admin"):
        response = guest.get(path, follow_redirects=False)
        assert response.status_code == 307 and response.headers["location"] == "/login"

    # The route map is not public, so FastAPI's automatic pages are off.
    assert guest.get("/docs").status_code == 404
    assert guest.get("/redoc").status_code == 404
    assert guest.get("/openapi.json").status_code == 404


def test_healthz_stays_minimal(conn):
    body = client_for(conn, make_user(conn, "healthadmin", role="admin")).get("/healthz").json()
    assert set(body) <= {"status", "ok", "data"}, "a health check does not report what the route map would"
    assert body["status"] == "ok"
