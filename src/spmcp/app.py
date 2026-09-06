"""ASGI application: MCP streamable-HTTP endpoint + health check + auth."""

from __future__ import annotations

import hmac
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from .config import Settings
from .server import build_server
from .store import SyncStore

log = logging.getLogger(__name__)

ASGIApp = Callable[
    [dict[str, Any], Callable[[], Awaitable[Any]], Callable[[Any], Awaitable[None]]],
    Awaitable[None],
]


class TokenAuthMiddleware:
    """Pure-ASGI bearer-token gate.

    * ``/healthz`` is always open.
    * ``Authorization: Bearer <token>`` is accepted for any configured token.
    * Optionally ``/t/<token>/...`` carries the token in the path (for clients
      that cannot send custom headers); the prefix is stripped before routing.
    """

    def __init__(
        self,
        app: ASGIApp,
        tokens: list[str],
        *,
        allow_token_in_path: bool,
        disabled: bool,
        open_paths: tuple[str, ...] = ("/healthz",),
    ) -> None:
        self.app = app
        self.tokens = [t.encode() for t in tokens]
        self.allow_token_in_path = allow_token_in_path
        self.disabled = disabled
        self.open_paths = open_paths

    def _token_ok(self, candidate: str) -> bool:
        c = candidate.encode()
        return any(hmac.compare_digest(c, t) for t in self.tokens)

    async def __call__(self, scope, receive, send):  # type: ignore[no-untyped-def]
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path: str = scope.get("path", "")
        if path in self.open_paths or self.disabled:
            await self.app(scope, receive, send)
            return

        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        auth = headers.get("authorization", "")
        if auth.lower().startswith("bearer ") and self._token_ok(auth[7:].strip()):
            await self.app(scope, receive, send)
            return

        if self.allow_token_in_path and path.startswith("/t/"):
            rest = path[3:]
            token, sep, remainder = rest.partition("/")
            if sep and self._token_ok(token):
                new_path = "/" + remainder
                scope = dict(scope)
                scope["path"] = new_path
                scope["raw_path"] = new_path.encode()
                await self.app(scope, receive, send)
                return

        response = JSONResponse(
            {"error": "unauthorized", "detail": "Provide a valid bearer token"},
            status_code=401,
            headers={"WWW-Authenticate": 'Bearer realm="spmcp"'},
        )
        await response(scope, receive, send)


def create_app(settings: Settings, store: SyncStore | None = None):
    settings.validate_runtime()
    store = store or SyncStore.from_settings(settings)
    server = build_server(store, settings)

    @server.custom_route("/healthz", methods=["GET"], include_in_schema=False)
    async def healthz(_: Request) -> Response:
        return JSONResponse({"status": "ok", "client_id": store.identity.client_id})

    hosts = settings.allowed_hosts
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=bool(hosts),
        allowed_hosts=hosts,
        allowed_origins=[],
    )
    mcp_app = server.streamable_http_app(
        streamable_http_path=settings.mcp_path,
        json_response=True,
        stateless_http=True,
        transport_security=security,
    )
    return TokenAuthMiddleware(
        mcp_app,
        settings.auth_tokens,
        allow_token_in_path=settings.mcp_allow_token_in_path,
        disabled=settings.mcp_auth_disabled,
    )
