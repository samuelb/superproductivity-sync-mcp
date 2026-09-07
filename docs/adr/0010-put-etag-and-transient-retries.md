# ADR-0010: Take the revision from the PUT response and retry transient WebDAV failures

- **Status:** Accepted (amends ADR-0005)
- **Date:** 2026-09-07
- **Sources:** `src/superproductivity_sync_mcp/store.py` (`mutate`), `src/superproductivity_sync_mcp/webdav.py`
  (`_request`, `put`); Nextcloud returns `OC-ETag` on `PUT` (`dav` app, `File::put`)

## Context

ADR-0005 step 6 re-downloaded the whole file after every upload to learn the
new revision and to compare hashes. On a multi-MB sync file that doubled the
download volume of every write and added latency to each tool call. Nextcloud
already returns the canonical strong etag of the new revision in the `PUT`
response, and the conditional `If-Match` on the next write catches any
mismatch anyway. Separately, a single connection error or a `503` from
Nextcloud maintenance mode failed the tool call immediately.

## Decision

- After a successful `PUT`, the strong `OC-ETag` (or strong `ETag`) of the
  response becomes the cached revision. No re-download by default.
- `SP_VERIFY_UPLOAD=true` restores the full re-download and md5 comparison as
  an opt-in diagnostic. It is also used automatically when a server that
  otherwise speaks strong etags returns none on `PUT`.
- `NextcloudDav._request` retries transient failures with exponential backoff
  (`HTTP_MAX_RETRIES`, default 2, base 0.5 s):
  - `GET`, `PROPFIND`, `MKCOL`: any transport error and HTTP 502/503/504.
  - `PUT`: only errors raised before the request was sent (connection
    refused/timeout) and HTTP 503. A read timeout or a 502/504 after a `PUT`
    may mean the server processed it; repeating it would race our own write,
    and a subsequent 412 would make `mutate` re-run the mutation on top of
    its own result (duplicate entity). Those errors fail the tool call.
- CPU-bound work (decode, encode, snapshot copy) runs in a worker thread so the
  event loop keeps serving other tool calls.

## Consequences

- One download per write instead of two; the cached snapshot after a write is
  the envelope we uploaded plus the server's etag.
- Brief Nextcloud hiccups are absorbed; a `PUT` interrupted mid-flight still
  surfaces as an error the agent has to report to the user.
- ADR-0005 step 6 is replaced by this record; its other steps stand.
