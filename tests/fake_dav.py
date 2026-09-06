"""In-process fake Nextcloud WebDAV endpoint (ASGI) for tests.

Supports GET / PUT (If-Match, If-None-Match: *) / PROPFIND / MKCOL and emits
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
    # Set to a callable to mutate state between the client's GET and PUT.
    on_put: object = None

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
            if path.rstrip("/") + "/" in {p.rsplit("/", 1)[0] + "/" for p in self.files} or path in self.files:
                etag = self.etag(path) if path in self.files else '"dir"'
                body = (
                    '<?xml version="1.0"?><d:multistatus xmlns:d="DAV:"><d:response>'
                    f"<d:href>{path}</d:href><d:propstat><d:prop><d:getetag>{etag}</d:getetag>"
                    "</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response></d:multistatus>"
                )
                return Response(body, status_code=207, media_type="application/xml")
            return Response(status_code=404)
        if method == "PUT":
            headers = {k.lower(): v for k, v in request.headers.items()}
            self.puts.append((path, headers))
            if callable(self.on_put):
                self.on_put(self, path)
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
