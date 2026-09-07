# ADR-0012: Keep a week of timestamped backups instead of one `.bak`

- **Status:** Accepted (amends ADR-0005 step 3)
- **Date:** 2026-09-07
- **Sources:** `src/superproductivity_sync_mcp/store.py` (`mutate`, `_prune_backups`),
  `src/superproductivity_sync_mcp/syncfile.py` (`backup_file_name`, `backup_timestamp`),
  `src/superproductivity_sync_mcp/webdav.py` (`list_names`, `delete`)

## Context

ADR-0005 wrote the previous content to a single `sync-data.json.bak` before
each upload, like the app does. One slot only covers the very last write: a
bad change noticed an hour and three tool calls later has no recovery
artifact left, and the app's own sync overwrites the same file name.

## Decision

- Each write stores the previous raw content as
  `sync-data.json.<YYYYMMDD>T<HHMMSS>Z.bak` (UTC, second precision, no
  characters that upset Nextcloud or Windows clients). The name sorts
  chronologically and needs no server metadata to interpret.
- After a successful upload the store lists the sync folder (PROPFIND Depth 1)
  and deletes backups whose name is older than `SP_BACKUP_RETENTION_DAYS`
  (default 7, 1–365). Pruning runs at most once an hour per process and is
  best effort: a failure is logged and never fails the write.
- Only files matching the timestamped pattern are ever deleted. The legacy
  `sync-data.json.bak` and anything else in the folder are left alone.
- `SP_WRITE_BACKUP=false` disables both the backup and the pruning.

## Consequences

- Recovery is possible for any write of the last week: download the backup
  from before the bad change and PUT it as `sync-data.json` (the app then
  reconciles through its vector clocks like after any conflict).
- Storage grows with write frequency times file size for a week; for a
  personal task list with a multi-MB file and tens of writes a day this is
  in the low hundreds of MB, well within a Nextcloud quota.
- `DELETE` joined the idempotent retry set; `NotFound` on delete is success.

## Rejected alternatives

- **Nextcloud file versioning.** Versions are only kept for files changed by
  a user through Nextcloud itself; the auto-expire policy is instance-wide and
  not under this server's control, and the app's writes churn the same file.
- **Rotating `.bak.1` … `.bak.N`.** Needs N renames per write (MOVE) and gives
  a count, not a time window; the user asked for a week.
