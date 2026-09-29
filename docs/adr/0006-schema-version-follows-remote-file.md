# ADR-0006: Stamp operations with the schema version of the remote file

- **Status:** Accepted
- **Date:** 2026-09-07
- **Sources:** `src/superproductivity_sync_mcp/store.py` (`_build_envelope`, `mutate`),
  `src/superproductivity_sync_mcp/syncfile.py` (`MAX_SCHEMA_VERSION`); app
  `remote-op-block.util.ts`, `packages/shared-schema/src/schema-version.ts`

## Context

Every operation carries `schemaVersion`. A client blocks (cursor frozen, "update
your app") on any op whose version is newer than its own `CURRENT_SCHEMA_VERSION`
(4 in 18.21.x and 18.22.x), and migrates older ones. The devices themselves already have to
agree on a version to sync with each other.

## Decision

- Use the `schemaVersion` found in the downloaded `sync-data.json` for the
  envelope and for every emitted op, instead of a constant baked into this
  server.
- Refuse to write while that version is higher than `MAX_SCHEMA_VERSION` (4),
  the newest schema whose reducers and payloads were verified for this server.
  Reads keep working. (Added 2026-09-29.)

## Consequences

- The server never fences devices that are at least as new as the last writer.
- Payload shapes must stay valid for the versions in use; the actions emitted
  here have had stable payloads since schema 1. If a future app migration
  changes a payload we emit, this server must learn the new shape.
- Once devices upgrade to an app with a newer schema, the server is read-only
  until someone re-verifies every emitted action against that app version and
  raises `MAX_SCHEMA_VERSION`. A stale server can no longer write a snapshot
  that a newer app would misread.
