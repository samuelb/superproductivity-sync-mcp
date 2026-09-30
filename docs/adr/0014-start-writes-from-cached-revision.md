# ADR-0014: Start writes from the cached revision when its etag is current

- **Status:** Accepted (amends ADR-0005 step 1)
- **Date:** 2026-09-30
- **Sources:** `src/superproductivity_sync_mcp/store.py` (`_revision_for_write`, `mutate`)

## Context

ADR-0005 downloads `sync-data.json` at the start of every write. An agent
often issues several writes in a row, and after each one the store already
holds the exact revision it uploaded, with the strong etag Nextcloud returned
for it (ADR-0010). Downloading it again costs a multi-MB GET and a decode per
write for no new information.

## Decision

- A write starts from the cached revision when the cache has a strong etag and
  a PROPFIND (Depth 0) returns that same etag; otherwise it downloads the file
  as before. A failed PROPFIND also falls back to the download.
- Nothing else changes: the mutation still runs on a deep copy, the backup is
  still the raw content of the revision being replaced, and the upload is
  still conditional on that etag.

## Consequences

- Consecutive writes cost a PROPFIND instead of a GET each.
- Correctness does not depend on the pre-check: if another device writes
  between the PROPFIND and the PUT, `If-Match` fails with 412 and the write is
  retried from a fresh download (ADR-0005 step 5). The cost of that race is
  one wasted backup upload.
- Servers without strong etags never take this path.

## Rejected alternatives

- **Trust the cache within `SP_CACHE_TTL_SECONDS` without a PROPFIND.** Also
  correct thanks to `If-Match`, but a device syncing in that window turns every
  write into a wasted backup upload plus a retry; the PROPFIND is cheap.
