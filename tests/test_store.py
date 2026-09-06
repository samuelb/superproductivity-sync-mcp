import pytest

from spmcp import mutations as m
from spmcp.store import ConflictError

from .conftest import BASE, decode_remote


async def test_create_task_writes_op_and_snapshot(store, fake_dav):
    out = {}

    def fn(ctx):
        out.update(m.create_task(ctx, title="Call Bob", due_day="today", tag_ids=["g1"]))

    await store.mutate(fn)
    env = decode_remote(fake_dav)
    assert env["syncVersion"] == 8
    assert env["clientId"] == "M_test01"
    assert env["vectorClock"] == {"E_abc123": 40, "A_def456": 12, "M_test01": 1}
    assert env["oldestOpSyncVersion"] == 7
    op = env["recentOps"][-1]
    assert op["a"] == "HA" and op["o"] == "CRT" and op["e"] == "TASK" and op["sv"] == 8
    assert op["d"] == out["id"] and op["ds"] == [out["id"]]
    assert op["c"] == "M_test01" and op["v"] == env["vectorClock"] and op["s"] == 4
    payload = op["p"]["actionPayload"]
    assert payload["workContextType"] == "PROJECT" and payload["workContextId"] == "INBOX_PROJECT"
    assert payload["task"]["id"] == out["id"]
    assert payload["task"]["dueDay"] == "2026-09-07"
    state = env["state"]
    assert out["id"] in state["task"]["entities"]
    assert state["project"]["entities"]["INBOX_PROJECT"]["taskIds"][0] == out["id"]
    assert state["tag"]["entities"]["g1"]["taskIds"][0] == out["id"]
    # local-only sync settings stripped, provider nulled
    assert "syncInterval" not in state["globalConfig"]["sync"]
    assert state["globalConfig"]["sync"]["syncProvider"] is None
    # backup was written with the previous content and the PUT was conditional
    paths = [p for p, _ in fake_dav.puts]
    assert f"{BASE}/sync-data.json.bak" in paths
    main_put = [h for p, h in fake_dav.puts if p == f"{BASE}/sync-data.json"][0]
    assert main_put["if-match"].startswith('"')
    # identity counter persisted
    assert store.identity.counter == 1


async def test_two_mutations_increment_clock(store, fake_dav):
    await store.mutate(lambda ctx: m.create_task(ctx, title="A"))
    await store.mutate(lambda ctx: m.create_project(ctx, title="P"))
    env = decode_remote(fake_dav)
    assert env["syncVersion"] == 9 and env["vectorClock"]["M_test01"] == 2
    assert [op["a"] for op in env["recentOps"]] == ["HU", "HA", "PA"]
    assert env["recentOps"][-1]["v"]["M_test01"] == 2


async def test_conflict_retry(store, fake_dav):
    calls = {"n": 0}
    original = fake_dav.files[f"{BASE}/sync-data.json"]

    def on_put(dav, path):
        # Another client writes between our GET and the first PUT.
        if path.endswith("sync-data.json") and calls["n"] == 0:
            calls["n"] += 1
            dav.files[path] = original.replace(b'"syncVersion":7', b'"syncVersion":8')

    fake_dav.on_put = on_put
    await store.mutate(lambda ctx: m.create_task(ctx, title="Retry me"))
    env = decode_remote(fake_dav)
    assert env["syncVersion"] == 9  # rebased on the concurrent version 8
    assert sum(1 for p, _ in fake_dav.puts if p.endswith("sync-data.json")) == 2


async def test_conflict_exhaustion(store, fake_dav):
    def on_put(dav, path):
        if path.endswith("sync-data.json"):
            dav.files[path] = dav.files[path] + b" "

    fake_dav.on_put = on_put
    store.max_attempts = 2
    with pytest.raises(ConflictError):
        await store.mutate(lambda ctx: m.create_task(ctx, title="never"))


async def test_noop_mutation_does_not_write(store, fake_dav):
    with pytest.raises(m.MutationError):
        await store.mutate(lambda ctx: m.update_task(ctx, "t1"))
    assert not fake_dav.puts


async def test_cache_uses_etag_precheck(store, fake_dav):
    store.cache_ttl = 0.0
    await store.load()
    gets = fake_dav.gets
    await store.load()
    assert fake_dav.gets == gets and fake_dav.propfinds >= 1


async def test_recent_ops_trimmed(store, fake_dav):
    from spmcp import codec
    from spmcp.syncfile import MAX_RECENT_OPS

    from .conftest import base_envelope

    env = base_envelope()
    env["recentOps"] = [dict(env["recentOps"][0], id=f"id{i}", sv=i) for i in range(MAX_RECENT_OPS)]
    fake_dav.files[f"{BASE}/sync-data.json"] = codec.encode_sync_file(
        env, codec.PrefixFlags(False, False, 2), None
    ).encode()
    await store.mutate(lambda ctx: m.create_task(ctx, title="x"))
    out = decode_remote(fake_dav)
    assert len(out["recentOps"]) == MAX_RECENT_OPS and out["recentOps"][-1]["a"] == "HA"
    assert out["oldestOpSyncVersion"] == 1
