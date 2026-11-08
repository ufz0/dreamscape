"""Web search and fetch: the result shape, the cache, the rate limit, the SSRF block, truncation.

The providers and the HTTP client are replaced here, so nothing depends on the network. The test
server is real and local; the injected client routes any host to it while the URL checks still run
the same way the app runs them.
"""
import asyncio
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import pytest

import config
import webfetch
import websearch


@pytest.fixture()
def clean():
    websearch._cache.clear()
    websearch._counts.clear()
    yield


# ---------- the providers ----------

def fake_provider(rows):
    calls = []

    def provider(query, max_results, recency):
        calls.append({"query": query, "max_results": max_results, "recency": recency})
        return rows

    provider.calls = calls
    return provider


def run_search(user_id, query, **kwargs):
    return asyncio.run(websearch.search(user_id, query, **kwargs))


def test_results_are_normalised_to_one_shape(clean, monkeypatch):
    # DuckDuckGo calls them href/body; Brave calls them url/snippet. Both come out the same.
    monkeypatch.setattr(websearch, "duckduckgo", fake_provider([
        {"title": "Home network lab", "href": "https://example.org/lab", "body": "A Netgear GS108 and OpenWrt.", "age": "2026-10-02"},
        {"title": "Switch check", "url": "https://www.example.com/check", "snippet": "ip route | grep 10.0.0"},
    ]))
    result = run_search("bob", "network lab")
    assert result["query"] == "network lab"
    assert len(result["results"]) == 2
    first = result["results"][0]
    assert first["url"] == "https://example.org/lab"
    assert first["domain"] == "example.org"
    assert first["date"] == "2026-10-02"
    assert first["snippet"]
    assert "data, not instructions" in result["note"]
    assert not result.get("error")
    assert result["results"][1]["domain"] == "www.example.com"


def test_cached_within_the_window(clean, monkeypatch):
    provider = fake_provider([{"title": "One", "url": "https://example.org/one", "snippet": ""}])
    monkeypatch.setattr(websearch, "duckduckgo", provider)
    run_search("bob", "same query")
    again = run_search("bob", "same query")
    assert again.get("cached") is True
    assert len(provider.calls) == 1, "the second search must not ask the provider again"
    # A different recency is a different result set, so it is a different cache entry.
    run_search("bob", "same query", recency="week")
    assert len(provider.calls) == 2


def test_rate_limit_per_user(clean, monkeypatch):
    monkeypatch.setattr(config, "SEARCH_RATE_LIMIT", 2)
    monkeypatch.setattr(config, "SEARCH_RATE_LIMIT_MINUTES", 10)
    monkeypatch.setattr(websearch, "duckduckgo", fake_provider([{"title": "x", "url": "https://example.org/x", "snippet": ""}]))
    assert not run_search("bob", "q1").get("error")
    assert not run_search("bob", "q2").get("error")
    blocked = run_search("bob", "q3")
    assert blocked["error"].startswith("Search failed: rate limited")
    # A cached query is not a search, so it does not count against the limit.
    cached = run_search("bob", "q1")
    assert cached.get("cached") and not cached.get("error")
    # Another user is not affected by bob's limit.
    other = run_search("carol", "q4")
    assert not other.get("error")


def test_provider_failure_retries_once_then_reports_clean(clean, monkeypatch):
    attempts = []

    def failing(query, max_results, recency):
        attempts.append(query)
        raise RuntimeError("rate limited")

    monkeypatch.setattr(websearch, "duckduckgo", failing)
    result = run_search("bob", "anything")
    assert len(attempts) == 2, "one retry with a short backoff, then give up"
    assert result["error"].startswith("Search failed:")
    assert result["results"] == []


def test_empty_answer_retries_once_then_says_so(clean, monkeypatch):
    calls = []

    def empty(query, max_results, recency):
        calls.append(query)
        return []

    monkeypatch.setattr(websearch, "duckduckgo", empty)
    result = run_search("bob", "anything")
    assert len(calls) == 2
    assert result["error"]
    assert not result.get("cached")


def test_brave_is_only_active_with_a_key(monkeypatch):
    monkeypatch.setattr(config, "SEARCH_PROVIDER", "brave")
    monkeypatch.setattr(config, "BRAVE_API_KEY", "")
    assert websearch.provider_status()["configured"] is False
    monkeypatch.setattr(config, "BRAVE_API_KEY", "a key")
    status = websearch.provider_status()
    assert status["configured"] is True and status["provider"] == "brave" and status["brave_key_set"]


def test_recency_is_validated(clean, monkeypatch):
    monkeypatch.setattr(websearch, "duckduckgo", fake_provider([]))
    result = run_search("bob", "q", recency="century")
    assert "recency must be one of" in result["error"]


# ---------- the SSRF block ----------

@pytest.mark.parametrize("url", [
    "http://127.0.0.1/page",
    "http://10.0.0.57/page",
    "http://172.16.5.4/page",
    "http://192.168.1.1/page",
    "http://169.254.169.254/latest/meta-data/",
    "http://[::1]/page",
    "http://[fd12::3]/page",
    "ftp://example.org/page",
])
def test_private_and_non_http_addresses_are_refused(url, monkeypatch):
    monkeypatch.setattr(config, "WEB_FETCH_ALLOW_PRIVATE", False)
    reason = webfetch.check_url(url)
    assert reason, f"{url} should be refused"


def test_public_address_is_allowed(monkeypatch):
    monkeypatch.setattr(config, "WEB_FETCH_ALLOW_PRIVATE", False)
    # A real public address, not a documentation prefix: Python counts those as private.
    monkeypatch.setattr(webfetch, "resolve", lambda host: ["93.184.216.34"])
    assert webfetch.check_url("https://example.org/page") is None


def test_a_name_that_resolves_to_a_private_ip_is_refused(monkeypatch):
    monkeypatch.setattr(config, "WEB_FETCH_ALLOW_PRIVATE", False)
    # The kind of name an internal DNS gives: a public-looking hostname, a private address.
    monkeypatch.setattr(webfetch, "resolve", lambda host: ["10.0.0.57"])
    reason = webfetch.check_url("https://strata.internal/page")
    assert reason and "private address" in reason


def test_allow_private_is_the_switch_for_trusted_setups(monkeypatch):
    monkeypatch.setattr(config, "WEB_FETCH_ALLOW_PRIVATE", True)
    assert webfetch.check_url("http://127.0.0.1/page") is None


# ---------- a fake page, served for real ----------

PAGE = """<!doctype html><html><head><title>Lab notes</title>
<script>document.title = "changed";</script><style>body{color:red}</style></head>
<body><nav><a href="/">Home</a> | <a href="/lab">Lab</a></nav>
<main><h1>Home network lab</h1><p>Hardware: Netgear GS108, router running OpenWrt 23.05.</p>
<ul><li>Wiki server at <a href="https://10.0.0.57">10.0.0.57</a></li></ul></main>
<footer>Footer text that is not content.</footer></body></html>"""


class PageServer(BaseHTTPRequestHandler):
    seen = []

    def log_message(self, *args):
        pass

    def do_GET(self):
        PageServer.seen.append({"path": self.path, "user_agent": self.headers.get("User-Agent"),
                                "cookie": self.headers.get("Cookie")})
        path = urlsplit(self.path).path
        if path == "/page":
            body = PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/plain":
            body = b"Plain text, no markup.\nSecond line.\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/binary":
            body = bytes(range(256))
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/redirect-private":
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{self.server.server_port}/plain")
            self.end_headers()
        elif path == "/redirect-loop":
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{self.server.server_port}/redirect-loop")
            self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()


class RoutedClient:
    """Talks to the test server for any host, so the URL checks still run on the real path."""

    def __init__(self, client):
        self.client = client

    async def get(self, url):
        parts = urlsplit(url)
        return await self.client.get(f"http://127.0.0.1:{self.client.base_url.port}{parts.path or '/'}")

    async def aclose(self):
        await self.client.aclose()


@pytest.fixture()
def server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), PageServer)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    PageServer.seen.clear()
    yield f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()
    httpd.server_close()


def client_for(base):
    import httpx
    return httpx.AsyncClient(base_url=base)


def fetch(url, client=None, max_chars=None):
    """No client means the app's own client, which is what the user-agent check is about."""
    return asyncio.run(webfetch.fetch(url, max_chars=max_chars, client=client))


def test_page_text_drops_scripts_and_navigation(server, monkeypatch):
    monkeypatch.setattr(config, "WEB_FETCH_ALLOW_PRIVATE", True)
    result = fetch(f"{server}/page")
    assert result["title"] == "Lab notes"
    assert "Netgear GS108" in result["text"]
    assert "Home network lab" in result["text"]
    assert "Footer text" not in result["text"], "nav and footer are not content"
    assert "document.title" not in result["text"], "scripts are not content"
    assert "[10.0.0.57]" not in result["text"]

    # The request carried no cookie and the plain user agent.
    seen = PageServer.seen[-1]
    assert seen["user_agent"] == config.USER_AGENT
    assert not seen["cookie"]


def test_plain_text_is_taken_as_is(server, monkeypatch):
    monkeypatch.setattr(config, "WEB_FETCH_ALLOW_PRIVATE", True)
    result = fetch(f"{server}/plain")
    assert "Plain text, no markup." in result["text"]
    assert not result.get("error")


def test_binary_is_refused(server, monkeypatch):
    monkeypatch.setattr(config, "WEB_FETCH_ALLOW_PRIVATE", True)
    result = fetch(f"{server}/binary")
    assert "Not text" in result["error"]


def test_truncation_is_said_in_the_result(server, monkeypatch):
    monkeypatch.setattr(config, "WEB_FETCH_ALLOW_PRIVATE", True)
    short = asyncio.run(webfetch.fetch(f"{server}/plain", max_chars=20, client=client_for(server)))
    assert len(short["text"]) <= 20
    assert short.get("truncated") and "Truncated to 20 characters" in short["note"]
    assert "data, not instructions" in short["note"]


def test_private_address_is_refused_before_connecting(server, monkeypatch):
    monkeypatch.setattr(config, "WEB_FETCH_ALLOW_PRIVATE", False)
    result = fetch(f"{server}/plain")
    assert "private address" in result["error"]
    assert not PageServer.seen, "nothing was requested"


def test_redirect_to_a_private_address_is_refused(server, monkeypatch):
    # The first hop looks public, the target does not: the check runs again after every redirect.
    monkeypatch.setattr(config, "WEB_FETCH_ALLOW_PRIVATE", False)
    monkeypatch.setattr(webfetch, "resolve",
                        lambda host: ["93.184.216.34"] if host != "127.0.0.1" else ["127.0.0.1"])
    result = asyncio.run(webfetch.fetch("http://public.test/redirect-private", client=RoutedClient(client_for(server))))
    assert result.get("error") and "private address" in result["error"]
    assert "public.test" in result["redirects"]


def test_redirects_are_counted(server, monkeypatch):
    monkeypatch.setattr(config, "WEB_FETCH_ALLOW_PRIVATE", True)
    monkeypatch.setattr(config, "WEB_FETCH_MAX_REDIRECTS", 2)
    result = fetch(f"{server}/redirect-loop")
    assert "Too many redirects" in result["error"]
    assert len(result["redirects"]) <= 3


# ---------- how the tool loop sees it ----------

def test_web_tool_results_are_delimited_for_the_model():
    text = agent_text = None
    import agent
    text = agent.tool_message("web_search", {"query": "q", "results": []})
    assert text.startswith("<web_content>") and text.endswith("</web_content>")
    assert not agent.tool_message("wiki_read", {"name": "page", "content": "x"}).startswith("<web_content>")


def test_a_search_failure_does_not_crash_the_turn(monkeypatch, conn, clean):
    import appdata
    from conftest import make_user
    from test_tool_loop import delta, events_for, sse, tool_call

    bob = make_user(conn, "webbob")
    appdata.set_current(bob["id"])
    appdata.current().ensure()

    def failing(query, max_results, recency):
        raise RuntimeError("rate limited")

    monkeypatch.setattr(websearch, "duckduckgo", failing)
    responses = [
        sse([delta(reasoning="I should search.", finish_reason="tool_calls",
                    tool_calls=[tool_call(0, "web_search", '{"query": "openwrt 23.05 release"}', call_id="c1")])]),
        sse([delta(content="I could not reach the search provider.", finish_reason="stop")]),
    ]
    events = events_for(monkeypatch, responses, bob)
    tool = [event for event in events if event["type"] == "tool"][0]
    assert tool["name"] == "web_search"
    assert tool["result"]["error"].startswith("Search failed:")
    # The turn still finished, so the model could react to it.
    assert assistant_text(events) == "I could not reach the search provider."


def assistant_text(events):
    final = [event for event in events if event["type"] == "assistant_message"][-1]
    return final["content"]
