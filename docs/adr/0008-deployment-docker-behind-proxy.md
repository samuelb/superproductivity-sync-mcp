# ADR-0008: Deployment as a non-root container behind an external reverse proxy

- **Status:** Accepted
- **Date:** 2026-09-07
- **Sources:** `Dockerfile`, `docker-compose.yml`, `deploy/Caddyfile`

## Context

The user runs a reverse proxy already; MCP clients require HTTPS.

## Decision

- Multi-stage image (`uv sync --frozen`), `python:3.12-slim`, non-root user,
  `tzdata` installed, `/data` volume for the client identity, health check on
  `/healthz`.
- Compose publishes **no host port**; the service joins a `proxy` network. A
  Traefik label set is provided commented out; an optional `caddy` profile
  bundles a proxy for setups without one (`deploy/Caddyfile`, no buffering).
- uvicorn runs with `proxy_headers` and `forwarded_allow_ips="*"` so scheme and
  client address come from the proxy.

## Consequences

- TLS, rate limiting and access logs are the proxy's job.
- Losing the `/data` volume regenerates the client id; the file keeps the old
  component forever (harmless but permanent), so the volume is named and
  documented.
