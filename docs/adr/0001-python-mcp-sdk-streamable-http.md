# ADR-0001: Python with the official MCP SDK over stateless streamable HTTP

- **Status:** Accepted
- **Date:** 2026-09-07
- **Sources:** `pyproject.toml`, `src/superproductivity_sync_mcp/app.py`, `src/superproductivity_sync_mcp/server.py`

## Context

The server must be reachable by ChatGPT connectors, Claude, Hermes Agent and
Pebble Index, all of which speak MCP over HTTP and sit behind our reverse proxy.
It also has to parse gzip, AES-GCM/Argon2id and a few megabytes of JSON, and be
easy to run as a small container.

## Decision

- Python 3.14 (current stable; 3.15 was still a release candidate), `mcp>=2.1,<3` (`MCPServer`, formerly FastMCP), `httpx`, `uvicorn`.
- Transport: streamable HTTP mounted at `/mcp`, `stateless_http=True`,
  `json_response=True`. Stateless mode needs no sticky sessions and survives
  proxy restarts; JSON responses avoid long-lived SSE streams through proxies.
- The SDK's Starlette app is wrapped by a pure-ASGI auth middleware so the
  SDK-managed lifespan (session manager) still runs.
- SSE transport is not exposed (deprecated in the spec).

## Consequences

- Any HTTP-capable MCP client works; stdio is not offered (the server is a
  remote service by design).
- Upgrading `mcp` across a major version needs a review of `server.py`/`app.py`
  (2.x already renamed the server class and result field names).

## Rejected alternatives

- TypeScript/Node: would allow importing Super Productivity's own packages, but
  those are not published as standalone libraries and the app's reducers are
  Angular/NgRx bound; the port is small enough to keep in Python.
- Stateful sessions with SSE resumption: unnecessary for short tool calls and
  harder to run behind generic proxies.
