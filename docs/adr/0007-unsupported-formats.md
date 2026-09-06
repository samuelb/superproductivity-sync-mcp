# ADR-0007: Rejected — split-file format, SuperSync, initial file creation

- **Status:** Accepted (records rejections)
- **Date:** 2026-09-07
- **Sources:** `src/spmcp/syncfile.py` (`validate_envelope`), `src/spmcp/store.py`

## Context

Super Productivity also offers an opt-in split format ("Surgical sync":
`sync-ops.json` + immutable `sync-state__*.json` snapshots with a v3 tombstone
in `sync-data.json`), a hosted SuperSync server, and can bootstrap an empty
folder.

## Decision

- Split format: **rejected for now**. A v3 tombstone is detected and reported
  with an actionable message. Supporting it needs the commit-point protocol,
  snapshot generations and migration resume logic.
- SuperSync API: **rejected**; different transport and E2EE per-op encryption.
- Creating the initial `sync-data.json`: **rejected**; a device must sync first
  so we inherit a valid state, archives, config and schema version.

## Consequences

Users must keep "Surgical sync" off for the folder this server uses. Revisit
the split format if the app makes it the default.
