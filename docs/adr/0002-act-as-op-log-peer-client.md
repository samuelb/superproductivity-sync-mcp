# ADR-0002: Act as a peer sync client on the v2 `sync-data.json` operation log

- **Status:** Accepted
- **Date:** 2026-09-07
- **Sources:** Super Productivity 18.21 source (`src/app/op-log/**`,
  `packages/sync-providers/src/file-based-sync-data.ts`), `src/superproductivity_sync_mcp/store.py`,
  `src/superproductivity_sync_mcp/syncfile.py`, `src/superproductivity_sync_mcp/ops.py`

## Context

Super Productivity ≥ 17 replaced the old "pfapi" model-file sync with an
operation log. With file providers (Nextcloud/WebDAV) the log is a single
`sync-data.json` (`FileBasedSyncData`, `version: 2`) holding a full state
snapshot, both archive partitions, a bounded `recentOps` buffer (2000), a vector
clock, a `syncVersion` counter and the last writer's `clientId`. Clients replay
ops they have not applied (deduplicated by op id) and use the snapshot only
when bootstrapping or after a detected gap. There is no server-side API.

## Decision

- The server is one more client: it owns a stable `clientId` (`M_xxxxxx`,
  persisted in `/data/client.json`) and increments its own vector clock
  component per operation, merging the remote clock first so every emitted op
  causally follows everything in the file.
- Each write appends compact operations (`{id: uuid7, a, o, e, d, ds, p:
  {actionPayload, entityChanges: []}, c, v, t, s, sv}`) **and** updates the
  snapshot (see ADR-0003), bumps `syncVersion`, sets `lastModified`/`clientId`,
  trims `recentOps` to 2000 and recomputes `oldestOpSyncVersion`.
- `schemaVersion` of emitted ops equals the file's `schemaVersion` (ADR-0006).
- Local-only sync settings are stripped from the snapshot exactly like the app
  (`syncInterval`, `isManualSyncOnly` removed, `syncProvider` nulled).
- The server never creates the initial file and never emits full-state ops
  (`SYNC_IMPORT`/`BACKUP_IMPORT`/`REPAIR`).

## Consequences

- Devices apply our changes through their normal reducers; no conflict dialog
  is raised for ordinary edits (LWW applies if a device edited the same entity
  concurrently).
- We can only emit action types whose reducers we have ported. Unsupported
  actions must be added together with their snapshot mirror.
- Archives (`archiveYoung`/`archiveOld`) are passed through untouched.

## Rejected alternatives

- Replacing the snapshot with a full-state op: shows a conflict dialog on every
  device and discards concurrent edits.
- Using the app's local REST API or plugin API: requires a running desktop app;
  the user explicitly wants Nextcloud as the integration point.
- Talking to a SuperSync server: not the user's setup.
