import asyncio

import httpx
import pytest

from superproductivity_sync_mcp.app import TokenAuthMiddleware

from .conftest import BASE


async def _echo(scope, receive, send):
    from starlette.responses import JSONResponse

    await JSONResponse({"path": scope["path"]})(scope, receive, send)


@pytest.fixture
def client():
    app = TokenAuthMiddleware(_echo, ["secret-token-1234567"], allow_token_in_path=True, disabled=False)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


async def test_health_open(client):
    r = await client.get("/healthz")
    assert r.status_code == 200


async def test_reject_without_token(client):
    r = await client.get("/mcp")
    assert r.status_code == 401 and r.headers["www-authenticate"].startswith("Bearer")


async def test_bearer_ok(client):
    r = await client.get("/mcp", headers={"Authorization": "Bearer secret-token-1234567"})
    assert r.status_code == 200 and r.json()["path"] == "/mcp"


async def test_path_token_rewrites(client):
    r = await client.get("/t/secret-token-1234567/mcp")
    assert r.status_code == 200 and r.json()["path"] == "/mcp"
    r = await client.get("/t/wrong/mcp")
    assert r.status_code == 401


async def test_mcp_end_to_end(store, fake_dav):
    """Drive the real MCP server over streamable HTTP with the SDK client."""
    from mcp.client.session import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    from superproductivity_sync_mcp.app import create_app
    from superproductivity_sync_mcp.config import Settings

    settings = Settings(
        nextcloud_url="https://cloud.example.com",
        nextcloud_user="alice",
        nextcloud_password="pw",
        mcp_auth_tokens="secret-token-1234567",
        mcp_allow_token_in_path=True,
        _env_file=None,
    )
    app = create_app(settings, store=store)
    transport = httpx.ASGITransport(app=app)
    # run the inner Starlette lifespan (session manager)
    inner = app.app
    async with inner.router.lifespan_context(inner):
        http = httpx.AsyncClient(
            transport=transport,
            base_url="http://t",
            headers={"Authorization": "Bearer secret-token-1234567"},
        )
        async with streamable_http_client("http://t/mcp", http_client=http) as (read, write, *_):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                names = {t.name for t in tools.tools}
                assert {"search", "fetch", "create_task", "get_today"} <= names
                res = await session.call_tool("get_today", {})
                assert not res.is_error
                assert res.structured_content["todo"][0]["id"] == "t1"
                res = await session.call_tool("create_task", {"title": "From MCP", "due_day": "today"})
                assert not res.is_error, res.content
                new_id = res.structured_content["id"]
                res = await session.call_tool("get_task", {"task_id": new_id})
                assert res.structured_content["dueDay"] == "2026-09-07"
                res = await session.call_tool("get_task", {"task_id": "missing"})
                assert res.is_error


def _run_main(monkeypatch, *, started: bool = True) -> dict:
    """Run main() with the probe, the store and uvicorn's serve() stubbed; return what they saw."""
    import superproductivity_sync_mcp.__main__ as entry

    seen: dict = {}

    class Store:
        async def aclose(self):
            seen["aclose_loop"] = asyncio.get_running_loop()

    async def probe(store):
        seen["probe_loop"] = asyncio.get_running_loop()

    async def serve(self, sockets=None):
        seen["serve_loop"] = asyncio.get_running_loop()
        seen["config"] = self.config
        self.started = started

    monkeypatch.setattr(entry.uvicorn.Server, "serve", serve)
    monkeypatch.setattr(entry, "create_app", lambda settings, store: object())
    monkeypatch.setattr(entry.SyncStore, "from_settings", classmethod(lambda cls, s: Store()))
    monkeypatch.setattr(entry, "_probe", probe)
    for k in ("NEXTCLOUD_URL", "NEXTCLOUD_USER", "NEXTCLOUD_PASSWORD", "MCP_AUTH_TOKENS"):
        monkeypatch.setenv(k, "https://cloud.example.com" if k == "NEXTCLOUD_URL" else "secret-token-1234567")
    entry.main()
    return seen


def test_uvicorn_access_log_disabled(monkeypatch):
    """Path tokens (/t/<token>/mcp) must never reach the access log."""
    assert _run_main(monkeypatch)["config"].access_log is False


def test_trusted_proxies_follow_the_setting(monkeypatch):
    assert _run_main(monkeypatch)["config"].forwarded_allow_ips == "*"
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", "172.18.0.0/16")
    assert _run_main(monkeypatch)["config"].forwarded_allow_ips == "172.18.0.0/16"


def test_probe_and_server_share_one_event_loop(monkeypatch):
    """The store's pooled keep-alive connections belong to the loop that opened them.

    Regression: the probe ran in an event loop of its own, and the first tool
    call after start-up failed with "Event loop is closed".
    """
    seen = _run_main(monkeypatch)
    assert seen["probe_loop"] is seen["serve_loop"] is seen["aclose_loop"]


def test_exit_status_when_server_does_not_start(monkeypatch):
    with pytest.raises(SystemExit) as ei:
        _run_main(monkeypatch, started=False)
    assert ei.value.code == 3


async def test_startup_probe_classifies_errors(store, fake_dav, caplog):
    import superproductivity_sync_mcp.__main__ as entry
    from superproductivity_sync_mcp.webdav import WebDavError

    # a healthy remote logs the sync file summary
    with caplog.at_level("INFO"):
        await entry._probe(store)
    assert "syncVersion=7" in caplog.text

    # configuration problems (no file, wrong password, bad credentials) abort start-up
    del fake_dav.files[f"{BASE}/sync-data.json"]
    with pytest.raises(SystemExit) as ei:
        await entry._probe(store)
    assert ei.value.code == 2

    # a merely unreachable Nextcloud only warns
    async def unreachable():
        raise WebDavError("PROPFIND failed: connection refused")

    store.dav.test_connection = unreachable
    with caplog.at_level("WARNING"):
        await entry._probe(store)
    assert "not reachable" in caplog.text


def _scope(path, headers=(), stype="http", client=("203.0.113.9", 4242)):
    return {
        "type": stype,
        "method": "GET",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": list(headers),
        "client": client,
    }


async def _call(app, scope):
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    await app(scope, receive, send)
    return sent


async def test_non_utf8_header_bytes_are_rejected_not_crashed():
    app = TokenAuthMiddleware(_echo, ["secret-token-1234567"], allow_token_in_path=False, disabled=False)
    sent = await _call(app, _scope("/mcp", [(b"authorization", b"Bearer \xff\xfe")]))
    assert sent[0]["status"] == 401
    sent = await _call(app, _scope("/mcp", [(b"x-junk", b"\xff"), (b"authorization", b"Bearer secret-token-1234567")]))
    assert sent[0]["status"] == 200


async def test_websocket_handshake_is_refused_without_reaching_the_app():
    hit = []

    async def inner(scope, receive, send):
        hit.append(scope["type"])

    app = TokenAuthMiddleware(inner, ["secret-token-1234567"], allow_token_in_path=False, disabled=False)
    sent = await _call(app, _scope("/mcp", stype="websocket"))
    assert sent == [{"type": "websocket.close", "code": 1008}] and hit == []
    await _call(app, {"type": "lifespan"})
    assert hit == ["lifespan"]


async def test_rejections_are_logged_without_the_token(client, caplog):
    from superproductivity_sync_mcp import app as app_module

    with caplog.at_level("WARNING", logger=app_module.__name__):
        await client.get("/mcp")
        await client.get("/mcp", headers={"Authorization": "Bearer wrong-token-attempt"})
        await client.get("/t/wrong-path-token/mcp")
        await client.get("/mcp", headers={"Authorization": "Bearer secret-token-1234567"})
    reasons = [r.getMessage() for r in caplog.records]
    assert len(reasons) == 3
    assert "no bearer token" in reasons[0] and "invalid bearer token" in reasons[1]
    assert "invalid path token" in reasons[2] and "/t/<redacted>/mcp" in reasons[2]
    assert "wrong-token-attempt" not in caplog.text and "wrong-path-token" not in caplog.text


async def test_rejection_log_is_throttled(caplog):
    from superproductivity_sync_mcp import app as app_module

    app = TokenAuthMiddleware(_echo, ["secret-token-1234567"], allow_token_in_path=False, disabled=False)
    with caplog.at_level("WARNING", logger=app_module.__name__):
        for _ in range(app_module.REJECT_LOG_MAX_PER_WINDOW + 30):
            await _call(app, _scope("/mcp"))
        assert len(caplog.records) == app_module.REJECT_LOG_MAX_PER_WINDOW
        app._reject_window_start -= app_module.REJECT_LOG_WINDOW_S  # the window elapses
        await _call(app, _scope("/mcp"))
    assert "Rejected 30 further" in caplog.records[-2].getMessage()
    assert caplog.records[-1].getMessage().startswith("Rejected GET /mcp from 203.0.113.9:4242")
