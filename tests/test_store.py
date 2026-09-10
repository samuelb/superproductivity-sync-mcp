import httpx
import pytest

from superproductivity_sync_mcp import mutations as m
from superproductivity_sync_mcp.store import ClientIdentity, ConflictError, SyncError, SyncStore
from superproductivity_sync_mcp.syncfile import backup_file_name, backup_timestamp
from superproductivity_sync_mcp.webdav import NextcloudDav

from .conftest import BASE, TZ, decode_remote
from .fake_dav import make_app


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
    assert any(backup_timestamp(p.rsplit("/", 1)[-1]) for p in paths)
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


async def test_locked_file_is_retried_after_redownload(store, fake_dav):
    # Nextcloud holds the file lock while the app uploads; the app's new
    # version lands before the lock clears. We must rebase on it, not fail.
    original = fake_dav.files[f"{BASE}/sync-data.json"]
    fake_dav.locked_puts = 3  # outlasts the client's own retries (max_retries=2)
    fake_dav.files[f"{BASE}/sync-data.json"] = original.replace(b'"syncVersion":7', b'"syncVersion":8')
    await store.mutate(lambda ctx: m.create_task(ctx, title="Locked"))
    env = decode_remote(fake_dav)
    assert env["syncVersion"] == 9
    puts = [p for p, _ in fake_dav.puts if p.endswith("sync-data.json")]
    assert len(puts) == 4  # 3 locked + 1 success
    assert fake_dav.gets == 2  # initial load + re-download after the lock


async def test_locked_file_exhaustion_is_a_conflict(store, fake_dav):
    fake_dav.locked_puts = 100
    store.max_attempts = 2
    with pytest.raises(ConflictError, match="attempts"):
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
    from superproductivity_sync_mcp import codec
    from superproductivity_sync_mcp.syncfile import MAX_RECENT_OPS

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


async def test_probe_reports_state(store):
    sf = await store.probe()
    assert sf.sync_version == 7 and sf.data["clientId"] == "E_abc123"


async def test_probe_fails_on_missing_file(store, fake_dav):
    del fake_dav.files[f"{BASE}/sync-data.json"]
    with pytest.raises(SyncError, match="Run a sync"):
        await store.probe()


async def test_probe_explains_wrong_user_id(fake_dav, tmp_path):
    from superproductivity_sync_mcp.webdav import NotFound

    transport = httpx.ASGITransport(app=make_app(fake_dav))
    dav = NextcloudDav("https://cloud.example.com", "bob", "bob", "pw", "/sp", transport=transport)
    store = SyncStore(dav, ClientIdentity.load(tmp_path, "M_test01"), password=None, allow_plaintext=False, tz=TZ)
    # the fake has no files under /files/bob/, so the root PROPFIND works but the GET 404s -> SyncError
    with pytest.raises(SyncError, match="Run a sync"):
        await store.probe()
    # a 404 on the DAV root itself names the variable to fix
    fake_dav.propfind_root_missing = True
    with pytest.raises(NotFound, match="NEXTCLOUD_USER"):
        await store.probe()


async def test_read_context_shares_snapshot_and_mutation_context_copies(store):
    sf = await store.load()
    ro = store.read_context_for(sf)
    assert ro.state is sf.state  # no deep copy for reads
    rw = store.context_for(sf)
    assert rw.state is not sf.state and rw.state == sf.state
    rw.state["task"]["entities"]["t1"]["title"] = "changed"
    assert sf.state["task"]["entities"]["t1"]["title"] == "Write report"


async def test_write_uses_put_etag_and_skips_verification_download(store, fake_dav):
    fake_dav.gets = 0
    await store.mutate(lambda ctx: m.create_task(ctx, title="A"))
    assert fake_dav.gets == 1  # the initial download only
    assert store._cache is not None and store._cache.strong_etag == fake_dav.etag(f"{BASE}/sync-data.json")
    # the cached revision is trusted by the next read (etag pre-check, no download)
    await store.load()
    assert fake_dav.gets == 1

    store.verify_upload = True
    fake_dav.gets = 0
    await store.mutate(lambda ctx: m.create_task(ctx, title="B"))
    assert fake_dav.gets == 2  # download + verification


async def test_codec_work_runs_off_the_event_loop(store, monkeypatch):
    import threading

    from superproductivity_sync_mcp import codec

    seen: set[int] = set()
    real_decode, real_encode = codec.decode_sync_file, codec.encode_sync_file

    def rec_decode(*a, **kw):
        seen.add(threading.get_ident())
        return real_decode(*a, **kw)

    def rec_encode(*a, **kw):
        seen.add(threading.get_ident())
        return real_encode(*a, **kw)

    monkeypatch.setattr(codec, "decode_sync_file", rec_decode)
    monkeypatch.setattr(codec, "encode_sync_file", rec_encode)
    await store.mutate(lambda ctx: m.create_task(ctx, title="threaded"))
    assert seen and threading.get_ident() not in seen


async def test_backup_is_timestamped_and_keeps_previous_content(store, fake_dav):
    store.now_ms = lambda: 1_788_000_000_000  # 2026-08-29T10:40:00Z
    original = fake_dav.files[f"{BASE}/sync-data.json"]
    await store.mutate(lambda ctx: m.create_task(ctx, title="A"))
    name = backup_file_name(1_788_000_000_000, 7)
    assert name == "sync-data.json.20260829T104000Z.sv7.bak"
    assert fake_dav.files[f"{BASE}/{name}"] == original
    assert backup_timestamp(name).isoformat() == "2026-08-29T10:40:00+00:00"
    assert backup_timestamp("sync-data.json.bak") is None and backup_timestamp("sync-data.json") is None


async def test_old_backups_are_pruned_after_a_week(store, fake_dav):
    now = 1_788_000_000_000
    store.now_ms = lambda: now
    day = 86_400_000
    keep = [backup_file_name(now - 6 * day), backup_file_name(now - 7 * day + 60_000)]
    drop = [backup_file_name(now - 7 * day - 60_000), backup_file_name(now - 30 * day)]
    for name in keep + drop:
        fake_dav.files[f"{BASE}/{name}"] = b"old"
    fake_dav.files[f"{BASE}/sync-data.json.bak"] = b"legacy"  # not ours to delete
    fake_dav.files[f"{BASE}/notes.txt"] = b"unrelated"

    await store.mutate(lambda ctx: m.create_task(ctx, title="A"))

    remaining = {p.rsplit("/", 1)[-1] for p in fake_dav.files}
    assert set(keep) <= remaining and not set(drop) & remaining
    assert {"sync-data.json.bak", "notes.txt", "sync-data.json", backup_file_name(now, 7)} <= remaining
    assert sorted(d.rsplit("/", 1)[-1] for d in fake_dav.deletes) == sorted(drop)

    # pruning is throttled: the next write within the hour does not list again
    propfinds = fake_dav.propfinds
    fake_dav.files[f"{BASE}/{drop[0]}"] = b"old"
    await store.mutate(lambda ctx: m.create_task(ctx, title="B"))
    assert fake_dav.propfinds == propfinds and f"{BASE}/{drop[0]}" in fake_dav.files

    # ... but does once the interval has passed
    store._last_prune -= 4000
    await store.mutate(lambda ctx: m.create_task(ctx, title="C"))
    assert f"{BASE}/{drop[0]}" not in fake_dav.files


async def test_prune_failure_does_not_fail_the_write(store, fake_dav, monkeypatch):
    async def boom():
        raise RuntimeError("listing broken")

    monkeypatch.setattr(store.dav, "list_names", boom)
    await store.mutate(lambda ctx: m.create_task(ctx, title="A"))
    assert decode_remote(fake_dav)["syncVersion"] == 8


async def test_no_backup_means_no_prune(store, fake_dav):
    store.write_backup = False
    await store.mutate(lambda ctx: m.create_task(ctx, title="A"))
    assert fake_dav.propfinds == 0 and all(not p.endswith(".bak") for p, _ in fake_dav.puts)


async def test_backup_names_carry_the_sync_version(store, fake_dav):
    """Two writes within one second must not share a backup name."""
    store.now_ms = lambda: 1_788_000_000_000
    before_first = fake_dav.files[f"{BASE}/sync-data.json"]
    await store.mutate(lambda ctx: m.create_task(ctx, title="A"))
    after_first = fake_dav.files[f"{BASE}/sync-data.json"]
    await store.mutate(lambda ctx: m.create_task(ctx, title="B"))
    first = backup_file_name(1_788_000_000_000, 7)
    second = backup_file_name(1_788_000_000_000, 8)
    assert first == "sync-data.json.20260829T104000Z.sv7.bak" and first != second
    assert fake_dav.files[f"{BASE}/{first}"] == before_first
    assert fake_dav.files[f"{BASE}/{second}"] == after_first
    assert backup_timestamp(first) == backup_timestamp("sync-data.json.20260829T104000Z.bak")


def test_backup_timestamp_ignores_impossible_dates():
    assert backup_timestamp("sync-data.json.99999999T999999Z.bak") is None
    assert backup_timestamp("sync-data.json.20260829T104000Z.svX.bak") is None
    assert backup_timestamp("sync-data.json.20260829T104000Z.sv12.bak") is not None


async def test_a_bogus_backup_name_does_not_stop_pruning(store, fake_dav):
    now = 1_788_000_000_000
    store.now_ms = lambda: now
    stale = backup_file_name(now - 30 * 86_400_000)
    fake_dav.files[f"{BASE}/{stale}"] = b"old"
    fake_dav.files[f"{BASE}/sync-data.json.99999999T999999Z.bak"] = b"odd"
    await store.mutate(lambda ctx: m.create_task(ctx, title="A"))
    assert f"{BASE}/{stale}" not in fake_dav.files
    assert f"{BASE}/sync-data.json.99999999T999999Z.bak" in fake_dav.files


def test_configured_client_id_must_have_the_minted_shape(tmp_path):
    with pytest.raises(ValueError, match="SP_CLIENT_ID"):
        ClientIdentity.load(tmp_path, "hello world!")  # 10+ chars: a reader accepts it, we must not mint it
    assert ClientIdentity.load(tmp_path, "M_ok_01").client_id == "M_ok_01"


def test_unreadable_counter_in_client_json_is_ignored(tmp_path, caplog):
    (tmp_path / "client.json").write_text('{"clientId": "M_test01", "counter": "many"}')
    with caplog.at_level("WARNING"):
        ident = ClientIdentity.load(tmp_path, None)
    assert ident.client_id == "M_test01" and ident.counter == 0
    assert "unreadable counter" in caplog.text
