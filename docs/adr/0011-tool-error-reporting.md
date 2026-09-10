# ADR-0011: Report anticipated tool failures as `ToolError` through one wrapper

- **Status:** Accepted
- **Date:** 2026-09-07
- **Sources:** `src/superproductivity_sync_mcp/server.py` (`tool_errors`), `tests/test_server.py`;
  `mcp.server.mcpserver.exceptions.ToolError` (SDK 2.x)

## Context

The MCP SDK distinguishes a deliberate `ToolError` (message returned to the
caller as an `isError` result, logged at INFO) from any other exception (a
crash: the caller sees only `Error executing tool <name>`, the server logs a
traceback at ERROR). Every failure this server raised — unknown id, invalid
day string, write conflict, unreachable Nextcloud — was a plain exception and
therefore a crash. The calling agent could not tell "task not found" from
"Nextcloud is down" and could not correct its call or retry.

## Decision

- Every tool is decorated with `tool_errors`, which maps the project's
  exception families to `ToolError` with a message the agent can act on:
  - `MutationError`, `timeutil.InputError`: the caller's mistake; the message is
    passed through unchanged. Not every `ValueError`: one raised by the codec,
    the op builder or the WebDAV client is a bug and must surface as a crash
    (traceback in the log) rather than be blamed on the caller.
  - `ConflictError`: "another device is writing; retry in a few seconds".
  - `AuthFailed`: credentials rejected; needs the operator.
  - other `SyncError` / `WebDavError`: Nextcloud unreachable or unreadable;
    an `HttpError` names the status and file only, the response body (Sabre
    error XML with the account path, a maintenance page with the host) goes to
    the server log (ADR-0004);
    retry later.
  - `StateError`, `SyncFormatError`: sync file content malformed; needs the
    operator.
- Infrastructure failures are additionally logged by the wrapper at WARNING
  (transient) or ERROR (operator action), since the SDK only logs a
  `ToolError` at INFO.
- Anything else still crashes: a traceback in the log is the right outcome
  for a bug.

## Consequences

- New tools must carry `@tool_errors` directly under `@server.tool(...)`;
  `tests/test_server.py` checks the tool count and the mapping.
- Domain code keeps raising its own exception types; only `server.py` knows
  about the SDK's error model.
- The messages name the actor who can fix the problem (caller, retry later,
  operator) so an agent can decide whether to retry, rephrase, or report.
