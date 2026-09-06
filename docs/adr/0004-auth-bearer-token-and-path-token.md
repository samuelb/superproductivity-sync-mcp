# ADR-0004: Bearer tokens, with an opt-in path token for header-less clients

- **Status:** Accepted
- **Date:** 2026-09-07
- **Sources:** `src/spmcp/app.py` (`TokenAuthMiddleware`), `src/spmcp/config.py`

## Context

The endpoint exposes personal data and accepts writes. Clients differ: Claude,
Hermes Agent and Pebble Index can send an `Authorization` header; ChatGPT
connectors support only OAuth or "no authentication". Pebble Index explicitly
does not support OAuth. TLS is terminated by the reverse proxy.

## Decision

- Static bearer tokens (`MCP_AUTH_TOKENS`, ≥ 16 chars, constant-time compare),
  several allowed so each client gets its own.
- Optional `MCP_ALLOW_TOKEN_IN_PATH`: `/t/<token>/...` is rewritten to `/...`
  after validation. Off by default; documented as a password-in-URL.
- `MCP_AUTH_DISABLED=true` exists for proxies that authenticate themselves.
- The server refuses to start with no token unless auth is explicitly disabled.
- `/healthz` is unauthenticated and reveals only the sync client id.
- DNS-rebinding protection of the SDK is enabled only when `MCP_ALLOWED_HOSTS`
  is set.

## Consequences

- Rotating a token = editing `.env` and restarting; the ChatGPT connector URL
  must be re-created when its token changes.
- No per-user identity: everyone with a token acts as the same Nextcloud user.

## Rejected alternatives

- OAuth 2.1 authorization server (SDK supports it): the only client that
  needs it is ChatGPT, and it adds a login UI, client registration and token
  storage for a single-user service. Revisit if a second person should use it.
- Nextcloud credentials passed by the client: they would have to be stored in
  every agent configuration and would grant full account access.
