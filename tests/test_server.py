"""Tool-level error reporting: anticipated failures reach the caller as ToolError."""

from __future__ import annotations

import pytest
from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError

from superproductivity_sync_mcp.config import Settings
from superproductivity_sync_mcp.server import build_server

from .conftest import BASE


@pytest.fixture
def server(store):
    settings = Settings(
        _env_file=None, nextcloud_url="https://cloud.example.com", nextcloud_user="alice", nextcloud_password="pw"
    )
    return build_server(store, settings)


async def call(server, name, **args):
    """Run a tool; return the ToolError for anticipated failures, else the result."""
    try:
        return await server.call_tool(name, args)
    except UnexpectedToolError:
        raise
    except ToolError as e:
        return e


async def test_tool_schemas_survive_the_error_wrapper(server):
    tools = {t.name: t for t in await server.list_tools()}
    assert len(tools) == 23
    schema = tools["create_task"].input_schema
    assert schema["required"] == ["title"]
    assert "due_day" in schema["properties"] and "parent_task_id" in schema["properties"]
    assert tools["create_task"].description.startswith("Create a task")
    assert tools["get_task"].annotations.read_only_hint is True


async def test_successful_call_returns_result(server):
    result = await call(server, "get_task", task_id="t1")
    assert not result.is_error and result.structured_content["title"] == "Write report"


async def test_unknown_id_is_reported_not_crashed(server):
    err = await call(server, "get_task", task_id="nope")
    assert isinstance(err, ToolError) and not isinstance(err, UnexpectedToolError)
    assert "Task 'nope' not found" in str(err)


async def test_invalid_input_is_reported_not_crashed(server, fake_dav):
    err = await call(server, "create_task", title="x", due_day="someday")
    assert isinstance(err, ToolError) and "Invalid day 'someday'" in str(err)
    assert not fake_dav.puts

    err = await call(server, "create_task", title="   ")
    assert isinstance(err, ToolError) and "Title must not be empty" in str(err)

    err = await call(server, "fetch", id="bogus:1")
    assert isinstance(err, ToolError) and "Unknown document id" in str(err)

    # an offset that would overflow date arithmetic is a validation error, not a crash
    err = await call(server, "create_task", title="x", due_day="+99999999999")
    assert isinstance(err, ToolError) and not isinstance(err, UnexpectedToolError)
    assert "too large" in str(err)
    assert not fake_dav.puts


async def test_internal_value_errors_are_not_blamed_on_the_caller(server, monkeypatch):
    """Only MutationError/InputError are the caller's mistake; any other ValueError is a bug."""
    from superproductivity_sync_mcp import queries

    def broken(*a, **kw):
        raise ValueError("internal invariant violated")

    monkeypatch.setattr(queries, "overview", broken)
    with pytest.raises(UnexpectedToolError):
        await server.call_tool("get_overview", {})


async def test_persistent_lock_is_reported_as_conflict(server, store, fake_dav):
    fake_dav.locked_puts = 100
    store.max_attempts = 2
    err = await call(server, "create_task", title="Blocked")
    assert isinstance(err, ToolError) and not isinstance(err, UnexpectedToolError)
    assert "Another device is writing" in str(err) and "Retry" in str(err)


async def test_unreachable_sync_file_is_reported(server, store, fake_dav):
    del fake_dav.files[f"{BASE}/sync-data.json"]
    store._cache = None
    err = await call(server, "get_overview")
    assert isinstance(err, ToolError) and "Could not reach or read the Nextcloud sync file" in str(err)


async def test_argument_validation_is_reported(server):
    err = await call(server, "create_task", title="x", time_estimate_minutes=-5)
    assert isinstance(err, ToolError) and not isinstance(err, UnexpectedToolError)


async def test_newer_schema_is_read_only(server, store, fake_dav):
    """ADR-0006: files from an app with a newer data schema are read, never written."""
    from superproductivity_sync_mcp import codec

    from .conftest import base_envelope

    env = base_envelope()
    env["schemaVersion"] = 5
    fake_dav.files[f"{BASE}/sync-data.json"] = codec.encode_sync_file(
        env, codec.PrefixFlags(False, False, 2), None
    ).encode()
    assert not isinstance(await call(server, "get_today"), ToolError)
    err = await call(server, "create_task", title="Blocked")
    assert isinstance(err, ToolError) and not isinstance(err, UnexpectedToolError)
    assert "schema 5" in str(err) and "update the server" in str(err)
    assert not fake_dav.puts
