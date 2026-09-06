import httpx
import pytest

from superproductivity_sync_mcp.app import TokenAuthMiddleware


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


def test_uvicorn_access_log_disabled(monkeypatch):
    """Path tokens (/t/<token>/mcp) must never reach the access log."""
    import superproductivity_sync_mcp.__main__ as entry

    captured = {}
    monkeypatch.setattr(entry.uvicorn, "run", lambda app, **kw: captured.update(kw))
    monkeypatch.setattr(entry, "create_app", lambda settings: object())
    for k in ("NEXTCLOUD_URL", "NEXTCLOUD_USER", "NEXTCLOUD_PASSWORD", "MCP_AUTH_TOKENS"):
        monkeypatch.setenv(k, "https://cloud.example.com" if k == "NEXTCLOUD_URL" else "secret-token-1234567")
    entry.main()
    assert captured["access_log"] is False
