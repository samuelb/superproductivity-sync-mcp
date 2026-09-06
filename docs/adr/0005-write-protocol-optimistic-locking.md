# ADR-0005: Write protocol — conditional PUT, backup first, verify, retry

- **Status:** Accepted
- **Date:** 2026-09-07
- **Sources:** `src/spmcp/store.py`, `src/spmcp/webdav.py`; app
  `file-based-sync-adapter.service.ts` (`_uploadOps`, `_uploadWithMismatchFallback`),
  `packages/sync-providers/src/file-based/webdav/webdav-api.ts`

## Context

Several devices and this server write the same file. Nextcloud returns a strong
canonical entity tag in `OC-ETag` (the HTTP `ETag` may be rewritten by Apache).
The app closes the GET→PUT race with `If-Match` and recovers corrupt writes
from `sync-data.json.bak`.

## Decision

Per mutation, under a process-wide lock:

1. GET `sync-data.json`; revision = `OC-ETag` if strong, else strong `ETag`,
   else md5 of the body.
2. Run the mutation on a deep copy; a mutation that emits no op writes nothing.
3. PUT the previous raw content to `sync-data.json.bak` (best effort).
4. PUT the new file with `If-Match: <strong etag>`; without a strong etag fall
   back to the app's re-GET hash comparison.
5. On `412` (or hash mismatch) discard, re-download and re-run the mutation —
   at most `SP_MAX_WRITE_ATTEMPTS` (3) times, then fail loudly.
6. Re-GET and compare md5 with what was sent (`SP_VERIFY_UPLOAD`).
7. Persist our vector-clock counter only after success.

Reads use a short cache (`SP_CACHE_TTL_SECONDS`) refreshed by a PROPFIND etag
check, so bursts of tool calls do not re-download a multi-MB file.

## Consequences

- No lost updates from concurrent devices as long as Nextcloud returns strong
  etags (it does); generic WebDAV without strong etags degrades to best effort
  like the app itself.
- Each write uploads the whole file twice (backup + new). Acceptable for a
  personal task list; `SP_WRITE_BACKUP=false` halves it.
- Mutations must be pure functions of the fresh remote state because they may
  be re-run.
