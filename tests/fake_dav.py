"""In-process fake Nextcloud WebDAV endpoint (ASGI) for tests.

Supports GET / PUT (If-Match, If-None-Match: *) / PROPFIND (Depth 0 and 1) /
MKCOL / DELETE and emits
Nextcloud-style ``OC-ETag`` headers. Files are keyed by URL path.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route


@dataclass
class FakeDav:
    files: dict[str, bytes] = field(default_factory=dict)
    puts: list[tuple[str, dict[str, str]]] = field(default_factory=list)
    gets: int = 0
    propfinds: int = 0
    deletes: list[str] = field(default_factory=list)
    # Set to a callable to mutate state between the client's GET and PUT.
    on_put: object = None
    # Simulate an unknown user id: PROPFIND on the DAV root returns 404.
    propfind_root_missing: bool = False
    # Number of upcoming PUTs on sync-data.json to reject with 423 (file lock).
    locked_puts: int = 0

    def etag(self, path: str) -> str:
        return '"' + hashlib.sha1(self.files[path]).hexdigest()[:20] + '"'

    async def handle(self, request: Request) -> Response:
        path = request.url.path
        method = request.method
        if method == "GET":
            self.gets += 1
            if path not in self.files:
                return Response(status_code=404)
            return Response(
                self.files[path],
                headers={"OC-ETag": self.etag(path), "ETag": self.etag(path)[:-1] + '-gzip"'},
            )
        if method == "PROPFIND":
            self.propfinds += 1
            prefix = path.rstrip("/") + "/"
            # The user's DAV root (trailing slash) always exists; other collections
            # exist when they contain a file.
            if self.propfind_root_missing and path.endswith("/"):
                return Response(status_code=404)
            if path.endswith("/") or path in self.files or any(f.startswith(prefix) for f in self.files):
                etag = self.etag(path) if path in self.files else '"dir"'
                hrefs = [(path, etag)]
                if request.headers.get("depth") == "1" and path not in self.files:
                    hrefs += [(f, self.etag(f)) for f in sorted(self.files) if f.startswith(prefix)]
                responses = "".join(
                    f"<d:response><d:href>{h}</d:href><d:propstat><d:prop><d:getetag>{e}</d:getetag>"
                    "</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>"
                    for h, e in hrefs
                )
                body = f'<?xml version="1.0"?><d:multistatus xmlns:d="DAV:">{responses}</d:multistatus>'
                return Response(body, status_code=207, media_type="application/xml")
            return Response(status_code=404)
        if method == "DELETE":
            self.deletes.append(path)
            if path not in self.files:
                return Response(status_code=404)
            del self.files[path]
            return Response(status_code=204)
        if method == "PUT":
            headers = {k.lower(): v for k, v in request.headers.items()}
            self.puts.append((path, headers))
            if callable(self.on_put):
                self.on_put(self, path)
            if self.locked_puts and path.endswith("sync-data.json"):
                self.locked_puts -= 1
                return Response(status_code=423, media_type="application/xml")
            if "if-match" in headers:
                if path not in self.files or headers["if-match"] != self.etag(path):
                    return Response(status_code=412)
            if headers.get("if-none-match") == "*" and path in self.files:
                return Response(status_code=412)
            existed = path in self.files
            self.files[path] = await request.body()
            return Response(status_code=204 if existed else 201, headers={"OC-ETag": self.etag(path)})
        if method == "MKCOL":
            return Response(status_code=201)
        return Response(status_code=405)


def make_app(dav: FakeDav) -> Starlette:
    return Starlette(routes=[Route("/{path:path}", dav.handle, methods=["GET", "PUT", "PROPFIND", "MKCOL", "DELETE"])])
