# Architecture Decision Records

One file per decision, numbered in the order they were written down, not
the order they were made. Each record states the context, the decision,
its consequences, and any alternative that was considered and rejected.
Rejected ideas get a record too, so they are not proposed again without
new information.

Read these before changing the architecture, the wire protocol, the
security model, or the release process. When you make a decision that
future work must respect, or reverse one recorded here, add a record or
mark the old one superseded in the same commit. Use `0000-template.md`.

| ADR | Title | Status |
| --- | ----- | ------ |
| [0001](0001-python-mcp-sdk-streamable-http.md) | Python with the official MCP SDK over stateless streamable HTTP | Accepted |
| [0002](0002-act-as-op-log-peer-client.md) | Act as a peer sync client on the v2 `sync-data.json` operation log | Accepted |
| [0003](0003-mirror-reducers-into-snapshot.md) | Mirror the app's reducers when updating the snapshot | Accepted |
| [0004](0004-auth-bearer-token-and-path-token.md) | Bearer tokens, with an opt-in path token for header-less clients | Accepted |
| [0005](0005-write-protocol-optimistic-locking.md) | Write protocol — conditional PUT, backup first, verify, retry | Accepted |
| [0006](0006-schema-version-follows-remote-file.md) | Stamp operations with the schema version of the remote file | Accepted |
| [0007](0007-unsupported-formats.md) | Rejected — split-file format, SuperSync, initial file creation | Accepted |
| [0008](0008-deployment-docker-behind-proxy.md) | Deployment as a non-root container behind an external reverse proxy | Accepted |
| [0009](0009-release-images-on-ghcr.md) | Build and publish container images with GitHub Actions to GHCR | Accepted |
