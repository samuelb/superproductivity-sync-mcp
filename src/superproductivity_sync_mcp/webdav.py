"""Minimal Nextcloud WebDAV client for the sync folder.

Mirrors the behaviour of the app's ``WebdavApi`` for the pieces we need:

* GET returns the body plus a *revision*: Nextcloud's canonical ``OC-ETag``
  header (survives Apache's ``-gzip`` suffixing), else a strong ``ETag``,
  else an md5 of the body.
* PUT is conditional: ``If-Match: <strong etag>`` when we hold one, or
  ``If-None-Match: *`` when creating. A ``412`` raises ``PreconditionFailed``.
  A ``404``/``409`` creates the parent collection once and retries.
* PROPFIND (Depth 0) gives the canonical etag without transferring the body.
* Transient failures are retried with a short backoff: connection errors and
  502/503/504 for the idempotent GET/PROPFIND/MKCOL; for PUT only errors
  raised before the request was sent and 503 (Nextcloud maintenance mode),
  because a PUT that may have been processed must not be repeated.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from urllib.parse import quote
from xml.etree import ElementTree as ET

import httpx

from . import __version__
from .codec import md5_hex

log = logging.getLogger(__name__)

_STRONG_ETAG_RE = re.compile(r'^"[\x21\x23-\x7e\x80-\xff]*"$')
_IDEMPOTENT = frozenset({"GET", "PROPFIND", "MKCOL"})
_RETRY_STATUS_IDEMPOTENT = frozenset({423, 502, 503, 504})
# 423: Nextcloud's transactional file locking while another client writes the
# same file. The server rejects the request without touching the file, so a PUT
# is safe to repeat.
_RETRY_STATUS_PUT = frozenset({423, 503})


class WebDavError(Exception):
    pass


class NotFound(WebDavError):
    pass


class PreconditionFailed(WebDavError):
    pass


class Locked(WebDavError):
    """HTTP 423: another client holds Nextcloud's file lock; nothing was written."""


class AuthFailed(WebDavError):
    pass


class HttpError(WebDavError):
    def __init__(self, status: int, method: str, path: str, body: str = ""):
        self.status = status
        super().__init__(f"HTTP {status} on {method} {path}: {body[:200]}")


@dataclass
class Downloaded:
    text: str
    rev: str
    strong_etag: str | None


def is_strong_etag(value: str | None) -> bool:
    return bool(value) and bool(_STRONG_ETAG_RE.match(value.strip()))


class NextcloudDav:
    def __init__(
        self,
        server_url: str,
        user: str,
        login: str,
        password: str,
        sync_folder: str,
        *,
        timeout: float = 120.0,
        transport: httpx.AsyncBaseTransport | None = None,
        max_retries: int = 2,
        retry_backoff: float = 0.5,
    ) -> None:
        self.max_retries = max(0, max_retries)
        self.retry_backoff = max(0.0, retry_backoff)
        server_url = server_url.rstrip("/")
        self.base_url = f"{server_url}/remote.php/dav/files/{quote(user.strip(), safe='')}/"
        self.sync_folder = sync_folder.strip().strip("/")
        self._client = httpx.AsyncClient(
            auth=(login, password),
            timeout=timeout,
            follow_redirects=False,
            transport=transport,
            headers={"User-Agent": f"superproductivity-sync-mcp/{__version__}"},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    # --- paths -------------------------------------------------------------

    def url_for(self, name: str) -> str:
        if ".." in name or "//" in name:
            raise ValueError("Invalid path")
        parts = [p for p in f"{self.sync_folder}/{name}".split("/") if p]
        return self.base_url + "/".join(quote(p, safe="") for p in parts)

    def folder_url(self) -> str:
        parts = [p for p in self.sync_folder.split("/") if p]
        return self.base_url + "/".join(quote(p, safe="") for p in parts)

    # --- low level ---------------------------------------------------------

    @staticmethod
    def _retryable_exception(method: str, e: httpx.HTTPError) -> bool:
        if isinstance(e, (httpx.ConnectError, httpx.ConnectTimeout)):
            return True  # nothing was sent yet; safe for every method
        return method in _IDEMPOTENT and isinstance(e, httpx.TransportError)

    @staticmethod
    def _retryable_status(method: str, status: int) -> bool:
        if method in _IDEMPOTENT:
            return status in _RETRY_STATUS_IDEMPOTENT
        return status in _RETRY_STATUS_PUT

    async def _request(self, method: str, url: str, **kw) -> httpx.Response:
        attempts = self.max_retries + 1
        for attempt in range(1, attempts + 1):
            try:
                resp = await self._client.request(method, url, **kw)
            except httpx.HTTPError as e:
                if attempt < attempts and self._retryable_exception(method, e):
                    await self._backoff(attempt, f"{method} {url}: {e}")
                    continue
                raise WebDavError(f"{method} failed: {e}") from e
            if attempt < attempts and self._retryable_status(method, resp.status_code):
                await self._backoff(attempt, f"{method} {url}: HTTP {resp.status_code}")
                continue
            break
        return self._raise_for_status(resp, url)

    async def _backoff(self, attempt: int, what: str) -> None:
        delay = self.retry_backoff * (2 ** (attempt - 1))
        log.warning("Transient WebDAV failure (%s); retry %d/%d in %.1fs", what, attempt, self.max_retries, delay)
        if delay:
            await asyncio.sleep(delay)

    @staticmethod
    def _raise_for_status(resp: httpx.Response, url: str) -> httpx.Response:
        if resp.status_code == 401:
            raise AuthFailed("Nextcloud rejected the credentials (HTTP 401)")
        if resp.status_code == 404:
            raise NotFound(url)
        if resp.status_code == 412:
            raise PreconditionFailed(url)
        if resp.status_code == 423:
            raise Locked(url)
        return resp

    @staticmethod
    def _rev_from_headers(resp: httpx.Response, body: str) -> tuple[str, str | None]:
        oc = resp.headers.get("oc-etag")
        if is_strong_etag(oc):
            return oc.strip(), oc.strip()
        et = resp.headers.get("etag")
        if is_strong_etag(et):
            return et.strip(), et.strip()
        return md5_hex(body), None

    # --- operations --------------------------------------------------------

    async def get(self, name: str) -> Downloaded:
        url = self.url_for(name)
        resp = await self._request("GET", url, headers={"Cache-Control": "no-cache"})
        if resp.status_code >= 300:
            raise HttpError(resp.status_code, "GET", name, resp.text)
        body = resp.content.decode("utf-8")
        if not body:
            raise WebDavError(f"Download of {name} returned an empty body")
        if body.lstrip().startswith("<") and "<html" in body[:512].lower():
            raise WebDavError(f"Download of {name} returned an HTML page instead of the file")
        rev, strong = self._rev_from_headers(resp, body)
        return Downloaded(body, rev, strong)

    async def get_etag(self, name: str) -> str | None:
        """Canonical etag via PROPFIND Depth 0, or None when unavailable."""
        url = self.url_for(name)
        body = '<?xml version="1.0"?><d:propfind xmlns:d="DAV:"><d:prop><d:getetag/></d:prop></d:propfind>'
        resp = await self._request(
            "PROPFIND",
            url,
            content=body,
            headers={"Depth": "0", "Content-Type": "application/xml; charset=utf-8"},
        )
        if resp.status_code != 207:
            return None
        try:
            root = ET.fromstring(resp.content)
        except ET.ParseError:
            return None
        for el in root.iter():
            if el.tag.endswith("}getetag") and el.text:
                v = el.text.strip()
                return v if is_strong_etag(v) else None
        return None

    async def put(
        self,
        name: str,
        data: str,
        *,
        if_match: str | None = None,
        create_only: bool = False,
    ) -> str | None:
        """Upload ``data``; returns the strong etag of the new revision when the server sends one."""
        if not data.strip():
            raise ValueError(f"Refusing to upload empty data to {name}")
        url = self.url_for(name)
        headers = {"Content-Type": "application/octet-stream"}
        if if_match:
            if not is_strong_etag(if_match):
                raise ValueError("if_match must be a strong entity tag")
            headers["If-Match"] = if_match
        elif create_only:
            headers["If-None-Match"] = "*"
        payload = data.encode("utf-8")
        try:
            resp = await self._request("PUT", url, content=payload, headers=headers)
        except NotFound:
            await self.mkcol(self.folder_url())
            resp = await self._request("PUT", url, content=payload, headers=headers)
        if resp.status_code == 409:
            await self.mkcol(self.folder_url())
            resp = await self._request("PUT", url, content=payload, headers=headers)
        if resp.status_code >= 300:
            raise HttpError(resp.status_code, "PUT", name, resp.text)
        for header in ("oc-etag", "etag"):
            value = resp.headers.get(header)
            if is_strong_etag(value):
                return value.strip()
        return None

    async def mkcol(self, url: str) -> None:
        try:
            resp = await self._request("MKCOL", url)
        except NotFound:
            return
        if resp.status_code in (201, 200, 301, 405, 409):
            return
        raise HttpError(resp.status_code, "MKCOL", url, resp.text)

    async def test_connection(self) -> None:
        """PROPFIND the user's DAV root: checks URL, credentials and user id."""
        body = '<?xml version="1.0"?><d:propfind xmlns:d="DAV:"><d:prop><d:resourcetype/></d:prop></d:propfind>'
        try:
            resp = await self._request(
                "PROPFIND",
                self.base_url,
                content=body,
                headers={"Depth": "0", "Content-Type": "application/xml; charset=utf-8"},
            )
        except NotFound as e:
            raise NotFound(
                f"DAV root {self.base_url} does not exist; NEXTCLOUD_USER must be the Nextcloud "
                "user id as shown in the WebDAV URL under Files > Settings"
            ) from e
        if resp.status_code not in (200, 207):
            raise HttpError(resp.status_code, "PROPFIND", "/", resp.text)
