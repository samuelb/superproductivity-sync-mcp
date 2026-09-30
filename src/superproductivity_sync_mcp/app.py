"""ASGI application: MCP streamable-HTTP endpoint + health check + auth."""

from __future__ import annotations

import hmac
import logging
import time
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


# Rejections are logged (client address, method, path with any path token
# redacted, reason) so a brute-force attempt is visible; after this many in a
# window the rest of the window is summarised in one line instead.
REJECT_LOG_MAX_PER_WINDOW = 20
REJECT_LOG_WINDOW_S = 60.0


class TokenAuthMiddleware:
    """Pure-ASGI bearer-token gate.

    * ``/healthz`` is always open.
    * ``Authorization: Bearer <token>`` is accepted for any configured token.
    * Optionally ``/t/<token>/...`` carries the token in the path (for clients
      that cannot send custom headers); the prefix is stripped before routing.
    * Only ``http`` and ``lifespan`` scopes pass; nothing behind the gate
      speaks websocket, so a handshake is refused rather than forwarded.
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
        self._reject_window_start = time.monotonic()
        self._reject_count = 0

    def _token_ok(self, candidate: str) -> bool:
        c = candidate.encode()
        return any(hmac.compare_digest(c, t) for t in self.tokens)

    @staticmethod
    def _bearer_token(scope: dict[str, Any]) -> str | None:
        """The bearer token of the request, "" for a non-bearer header, None when absent."""
        for k, v in scope.get("headers", []):
            if k.lower() == b"authorization":
                # Header values are raw bytes; latin-1 maps every byte and never raises.
                value = v.decode("latin-1")
                if value[:7].lower() == "bearer ":
                    return value[7:].strip()
                return ""
        return None

    @staticmethod
    def _redact_path(path: str) -> str:
        if not path.startswith("/t/"):
            return path
        _, sep, remainder = path[3:].partition("/")
        return "/t/<redacted>" + ("/" + remainder if sep else "")

    def _log_rejection(self, scope: dict[str, Any], path: str, reason: str) -> None:
        now = time.monotonic()
        if now - self._reject_window_start >= REJECT_LOG_WINDOW_S:
            suppressed = self._reject_count - REJECT_LOG_MAX_PER_WINDOW
            if suppressed > 0:
                log.warning("Rejected %d further unauthenticated requests in the last minute", suppressed)
            self._reject_window_start = now
            self._reject_count = 0
        self._reject_count += 1
        if self._reject_count > REJECT_LOG_MAX_PER_WINDOW:
            return
        client = scope.get("client")
        addr = f"{client[0]}:{client[1]}" if client else "unknown"
        log.warning("Rejected %s %s from %s: %s", scope.get("method", "?"), self._redact_path(path), addr, reason)

    async def __call__(
        self,
        scope: dict[str, Any],
        receive: Callable[[], Awaitable[Any]],
        send: Callable[[Any], Awaitable[None]],
    ) -> None:
        scope_type = scope["type"]
        if scope_type == "lifespan":
            await self.app(scope, receive, send)
            return
        if scope_type != "http":
            if scope_type == "websocket":
                await send({"type": "websocket.close", "code": 1008})
            return
        path: str = scope.get("path", "")
        if path in self.open_paths or self.disabled:
            await self.app(scope, receive, send)
            return

        token = self._bearer_token(scope)
        if token and self._token_ok(token):
            await self.app(scope, receive, send)
            return

        reason = "no bearer token" if token is None else "invalid bearer token"
        if self.allow_token_in_path and path.startswith("/t/"):
            rest = path[3:]
            path_token, sep, remainder = rest.partition("/")
            if sep and self._token_ok(path_token):
                new_path = "/" + remainder
                scope = dict(scope)
                scope["path"] = new_path
                scope["raw_path"] = new_path.encode()
                await self.app(scope, receive, send)
                return
            reason = "invalid path token"

        self._log_rejection(scope, path, reason)
        response = JSONResponse(
            {"error": "unauthorized", "detail": "Provide a valid bearer token"},
            status_code=401,
            headers={"WWW-Authenticate": 'Bearer realm="superproductivity-sync-mcp"'},
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
