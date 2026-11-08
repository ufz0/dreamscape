"""Per-user data isolation: no endpoint or tool can reach another user's data."""
from fastapi.testclient import TestClient

import accounts
import agent
import app
import appdata
from conftest import make_user


def client_for(conn, user):
    import app

    client = TestClient(app.app)
    token = accounts.create_session(conn, user)
    conn.commit()
    client.cookies.set("session", token)
    client.get("/")                            # loading a page is what sets the CSRF cookie
    client.headers["X-CSRF-Token"] = client.cookies.get("csrf")
    return client


def test_pages_and_api_require_a_session(conn):
    admin = make_user(conn, "gateadmin", role="admin")
    guest = TestClient(app.app)
    assert guest.get("/api/conversations").status_code == 401
    page = guest.get("/wiki", follow_redirects=False)
    assert page.status_code == 307 and page.headers["location"] == "/login"
    client = client_for(conn, admin)
    assert client.get("/api/conversations").status_code == 200
    # Non-admins are not allowed in the admin panel.
    normal = make_user(conn, "normaluser")
    assert client_for(conn, normal).get("/admin").status_code == 403
    assert client_for(conn, normal).get("/api/admin/users").status_code == 403
    assert client_for(conn, admin).get("/api/admin/users").status_code == 200


def test_healthz_needs_no_session(conn):
    """The only route without a session, because a container health check cannot log in."""
    response = TestClient(app.app).get("/healthz")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_wiki_tool_only_sees_its_own_users_pages(conn):
    admin = make_user(conn, "wikiaadmin", role="admin")
    bob = make_user(conn, "wikibob")

    appdata.set_current(admin["id"])
    appdata.current().ensure()
    agent.execute_tool("wiki_write", {"name": "admin-note", "content": "# Admin note\n\nOnly the admin knows this."})
    appdata.set_current(bob["id"])
    appdata.current().ensure()
    agent.execute_tool("wiki_write", {"name": "bob-note", "content": "# Bob note\n\nPrivate."})

    # A tool call for bob cannot read the admin's page, and lists only bob's pages.
    assert agent.execute_tool("wiki_read", {"name": "admin-note"})["error"]
    assert [page["name"] for page in agent.execute_tool("wiki_list", {})["pages"]] == ["bob-note"]
    assert agent.execute_tool("wiki_search", {"query": "admin knows"})["results"] == []
    # Traversal is still sandboxed to the current user's folder.
    assert agent.execute_tool("wiki_read", {"name": "../users/1/admin-note"})["error"]

    # And the same through the API.
    admin_client = client_for(conn, admin)
    bob_client = client_for(conn, bob)
    assert [page["name"] for page in admin_client.get("/api/wiki/list").json()["pages"]] == ["admin-note"]
    assert [page["name"] for page in bob_client.get("/api/wiki/list").json()["pages"]] == ["bob-note"]
    assert bob_client.get("/api/wiki/page", params={"name": "admin-note"}).status_code == 404
    assert bob_client.get("/api/wiki/search", params={"q": "admin knows"}).json()["results"] == []
    results = admin_client.get("/api/wiki/search", params={"q": "admin knows"}).json()["results"]
    assert results and all("admin-note" in item["file"] or item["file"] == "index.md" for item in results)


def test_conversations_are_not_shared(conn):
    admin = make_user(conn, "chataadmin", role="admin")
    bob = make_user(conn, "chatbob")

    admin_client = client_for(conn, admin)
    bob_client = client_for(conn, bob)

    created = admin_client.post("/api/conversations", json={}).json()["conversation"]
    admin_client.post(f"/api/conversations/{created['id']}/rename", json={"title": "admin chat"})
    assert bob_client.get(f"/api/conversations/{created['id']}/messages").status_code == 404
    assert bob_client.get("/api/conversations").json()["conversations"] == []
    assert [row["title"] for row in admin_client.get("/api/conversations").json()["conversations"]] == ["admin chat"]
    assert bob_client.delete(f"/api/conversations/{created['id']}").status_code == 404
    # Naming is the same path: bob cannot name a chat that is not his.
    assert bob_client.post(f"/api/conversations/{created['id']}/title", json={}).status_code == 404


def test_dream_runs_and_usage_are_per_user(conn):
    import dreamlog
    from conftest import add_usage

    admin = make_user(conn, "dreamadmin", role="admin")
    bob = make_user(conn, "dreambob")

    appdata.set_current(bob["id"])
    appdata.current().ensure()
    record = dreamlog.build_record("button", "test-model", {"per_transcript": "medium"}, bob)
    dreamlog.save(record)

    appdata.set_current(admin["id"])
    admin_client = client_for(conn, admin)
    assert admin_client.get("/api/dreams").json()["runs"] == []
    assert client_for(conn, bob).get("/api/dreams").json()["runs"][0]["id"] == record["id"]
    assert admin_client.get(f"/api/dreams/{record['id']}").status_code == 404

    add_usage(conn, bob["id"], 1000)
    admin_view = admin_client.get("/api/admin/usage", params={"period": "month"}).json()
    assert admin_view["totals"]["tokens"] == 1000
    assert [row["username"] for row in admin_view["per_user"]] == ["dreambob"]
    # The admin sees numbers only: no chat content in the response.
    assert "admins_cannot_read_content" in admin_view
    assert admin_client.get("/api/usage/me").json()["totals"]["tokens"] == 0
    assert client_for(conn, bob).get("/api/usage/me").json()["totals"]["tokens"] == 1000


def test_csrf_is_required_for_state_changing_requests(conn):
    admin = make_user(conn, "csrfaadmin", role="admin")
    client = client_for(conn, admin)
    client.headers.pop("X-CSRF-Token")
    assert client.post("/api/conversations", json={}).status_code == 403
    client.headers["X-CSRF-Token"] = "wrong-value"
    assert client.post("/api/conversations", json={}).status_code == 403
