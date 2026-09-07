"""Retry policy and etag handling of the WebDAV client."""

from __future__ import annotations

from collections import deque

import httpx
import pytest

from superproductivity_sync_mcp.webdav import Locked, NextcloudDav, WebDavError

from .conftest import BASE
from .fake_dav import FakeDav, make_app


class FlakyTransport(httpx.AsyncBaseTransport):
    """Fails the first requests as scripted (exception instance or status code), then delegates."""

    def __init__(self, inner: httpx.AsyncBaseTransport, script: list) -> None:
        self.inner = inner
        self.script = deque(script)
        self.requests: list[str] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request.method)
        if self.script:
            step = self.script.popleft()
            if isinstance(step, Exception):
                raise step
            return httpx.Response(step, request=request)
        return await self.inner.handle_async_request(request)


def make_dav(fake: FakeDav, script: list) -> tuple[NextcloudDav, FlakyTransport]:
    transport = FlakyTransport(httpx.ASGITransport(app=make_app(fake)), script)
    dav = NextcloudDav(
        "https://cloud.example.com", "alice", "alice", "pw", "/sp", transport=transport, retry_backoff=0.0
    )
    return dav, transport


async def test_get_retries_connect_errors_and_gateway_statuses(fake_dav):
    dav, t = make_dav(fake_dav, [httpx.ConnectError("refused"), 503])
    got = await dav.get("sync-data.json")
    assert got.text.startswith("pf_2__") and t.requests == ["GET", "GET", "GET"]


async def test_get_gives_up_after_max_retries(fake_dav):
    dav, t = make_dav(fake_dav, [httpx.ReadTimeout("slow")] * 3)
    with pytest.raises(WebDavError, match="GET failed"):
        await dav.get("sync-data.json")
    assert t.requests == ["GET"] * 3


async def test_put_retries_only_before_the_request_was_sent(fake_dav):
    # connect error: nothing was sent, safe to retry
    dav, t = make_dav(fake_dav, [httpx.ConnectError("refused")])
    etag = await dav.put("sync-data.json", "pf_2__{}", if_match=fake_dav.etag(f"{BASE}/sync-data.json"))
    assert t.requests == ["PUT", "PUT"] and etag == fake_dav.etag(f"{BASE}/sync-data.json")

    # 503 (maintenance mode) and 423 (file locked by another client) are retried as well
    for status in (503, 423):
        dav, t = make_dav(fake_dav, [status])
        await dav.put("sync-data.json", "pf_2__{}", if_match=fake_dav.etag(f"{BASE}/sync-data.json"))
        assert t.requests == ["PUT", "PUT"]

    # a lock that outlives the retries surfaces as Locked
    dav, t = make_dav(fake_dav, [423] * 3)
    with pytest.raises(Locked):
        await dav.put("sync-data.json", "pf_2__{}")
    assert t.requests == ["PUT"] * 3

    # a timeout while waiting for the answer may mean the PUT went through: never retried
    dav, t = make_dav(fake_dav, [httpx.ReadTimeout("slow")])
    with pytest.raises(WebDavError):
        await dav.put("sync-data.json", "pf_2__{}")
    assert t.requests == ["PUT"]

    # 502/504 from a proxy are ambiguous for PUT: not retried either
    dav, t = make_dav(fake_dav, [502])
    with pytest.raises(WebDavError):
        await dav.put("sync-data.json", "pf_2__{}")
    assert t.requests == ["PUT"]


async def test_put_returns_strong_etag_or_none(fake_dav):
    dav, _ = make_dav(fake_dav, [])
    assert (await dav.put("new.json", "x")) == fake_dav.etag(f"{BASE}/new.json")
    weak = httpx.Response(201, headers={"ETag": 'W/"abc"'})
    dav2, t = make_dav(fake_dav, [weak.status_code])
    assert (await dav2.put("other.json", "x")) is None  # scripted response carries no etag


async def test_etag_parsing_prefers_oc_etag_and_rejects_weak(fake_dav):
    from superproductivity_sync_mcp.webdav import is_strong_etag

    assert is_strong_etag('"abc"') and not is_strong_etag('W/"abc"') and not is_strong_etag(None)
    dav, _ = make_dav(fake_dav, [])
    got = await dav.get("sync-data.json")
    # the fake sends a mangled Apache-style ETag next to the canonical OC-ETag
    assert got.strong_etag == fake_dav.etag(f"{BASE}/sync-data.json")
    assert await dav.get_etag("sync-data.json") == got.strong_etag

    from superproductivity_sync_mcp.webdav import NotFound

    with pytest.raises(NotFound):
        await dav.get("missing.json")
    with pytest.raises(NotFound):
        await dav.get_etag("missing.json")


async def test_list_names_and_delete(fake_dav):
    dav, _ = make_dav(fake_dav, [])
    fake_dav.files[f"{BASE}/sync-data.json.20260901T000000Z.bak"] = b"x"
    fake_dav.files[f"{BASE}/with space.txt"] = b"y"
    fake_dav.files[f"{BASE}/sub/nested.json"] = b"z"  # one level deeper: not listed
    fake_dav.files["/remote.php/dav/files/alice/other/sync-data.json"] = b"w"
    assert sorted(await dav.list_names()) == [
        "sync-data.json",
        "sync-data.json.20260901T000000Z.bak",
        "with space.txt",
    ]
    await dav.delete("with space.txt")
    await dav.delete("with space.txt")  # already gone: not an error
    assert f"{BASE}/with space.txt" not in fake_dav.files
