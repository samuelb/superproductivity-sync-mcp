# ADR-0015: Keep tool input schemas free of `$ref` and `$defs`

- **Status:** Accepted
- **Date:** 2026-10-20
- **Sources:** `src/superproductivity_sync_mcp/server.py` (`NEW_TASKS_SCHEMA`), `tests/test_server.py` (`test_tool_schemas_are_self_contained`), commit 0e2aa3e

## Context

0.2.0 added `create_tasks`, whose `list[NewTask]` parameter made Pydantic put
the item schema under `$defs` and reference it with `$ref`. Pebble Index then
hung on "loading MCP plugin" when recording a new note. Image `sha-5af5a56`
(0.2.0's dependencies, no `create_tasks`) loaded fine. No other tool's schema
had a `$ref`, and the handshake was otherwise unchanged. Not every client
resolves JSON Schema references, and a client that can't parse one tool may
fail to load the whole server.

## Decision

- Every tool's input schema is self-contained: no `$ref`, no `$defs`.
- A parameter typed with a nested model declares its schema inline with
  `pydantic.WithJsonSchema` (see `NEW_TASKS_SCHEMA`); Pydantic still validates
  the arguments against the model.
- A test asserts this for every registered tool.

## Consequences

The inline schema must repeat the field's constraints (`minItems`,
`maxItems`), since `WithJsonSchema` replaces what Pydantic would generate. A
nested model must not itself contain models, or `model_json_schema()` would
bring back `$defs`; the test catches that.

## Rejected alternatives

- Patching `tool.parameters` through the SDK's private `_tool_manager` after
  registration: it depends on SDK internals that 2.x minor releases change.
- Taking `list[dict]` and validating by hand: it loses the per-field schema
  that tells the agent what each item may contain.
