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
    assert len(tools) == 24
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


async def test_update_and_unschedule_through_the_tools(server, fake_dav):
    from .conftest import decode_remote

    res = await call(server, "update_task", task_id="t2", is_done=True, time_estimate_minutes=15)
    assert not isinstance(res, ToolError)
    res = await call(server, "unschedule_task", task_id="t2")
    assert not isinstance(res, ToolError)
    env = decode_remote(fake_dav)
    assert [op["a"] for op in env["recentOps"][-2:]] == ["HU", "HSX"]
    t2 = env["state"]["task"]["entities"]["t2"]
    assert t2["isDone"] is True and t2["timeEstimate"] == 900000 and "dueDay" not in t2


@pytest.mark.parametrize(
    ("body", "password", "detail"),
    [
        ({"version": 3, "format": "split"}, None, "Surgical sync"),
        ({"version": 2}, "other-password", "wrong password"),
        (None, None, "corrupt"),
    ],
    ids=["split-format", "wrong-password", "corrupt-body"],
)
async def test_unusable_sync_file_needs_the_operator(server, store, fake_dav, body, password, detail):
    """Regression: these were reported as 'retry later', so agents retried in vain."""
    from superproductivity_sync_mcp import codec

    if body is None:
        raw = "pf_C2__AAAAbm90IGd6aXA="  # compressed flag, not a gzip stream
    else:
        raw = codec.encode_sync_file(body, codec.PrefixFlags(False, password is not None, 2), password)
    fake_dav.files[f"{BASE}/sync-data.json"] = raw.encode()
    store.password = "configured-password" if password else None
    err = await call(server, "get_overview")
    assert isinstance(err, ToolError) and not isinstance(err, UnexpectedToolError)
    assert "needs the operator" in str(err) and "retry" not in str(err) and detail in str(err)


async def test_create_tasks_is_one_write_in_the_given_order(server, fake_dav):
    from .conftest import decode_remote

    before = len(fake_dav.puts)
    res = await call(
        server,
        "create_tasks",
        tasks=[{"title": "A", "due_day": "today"}, {"title": "B"}, {"title": "Sub", "parent_task_id": "t1"}],
    )
    assert not isinstance(res, ToolError)
    assert [t["title"] for t in res.structured_content["tasks"]] == ["A", "B", "Sub"]
    assert len([p for p, _ in fake_dav.puts[before:] if p.endswith("/sync-data.json")]) == 1
    env = decode_remote(fake_dav)
    assert [op["a"] for op in env["recentOps"][-3:]] == ["HA", "HA", "TA"]
    ents = env["state"]["task"]["entities"]
    inbox = env["state"]["project"]["entities"]["INBOX_PROJECT"]["taskIds"]
    assert [ents[i]["title"] for i in inbox[:2]] == ["A", "B"]  # top of the list, in the given order


async def test_create_tasks_is_all_or_nothing(server, fake_dav):
    before = len(fake_dav.puts)
    err = await call(server, "create_tasks", tasks=[{"title": "ok"}, {"title": "bad", "due_day": "someday"}])
    assert isinstance(err, ToolError) and not isinstance(err, UnexpectedToolError)
    assert "Task 2 ('bad')" in str(err) and "Invalid day" in str(err)
    assert len(fake_dav.puts) == before
