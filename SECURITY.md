# Security policy

This server holds a Nextcloud app password, optionally the sync-file
encryption password, and write access to your whole Super Productivity data.
Please report weaknesses privately.

## Supported versions

Fixes land on `main` and in the next release. Older releases are not patched.

## Reporting a vulnerability

Use GitHub's private reporting: **Security → Report a vulnerability** on this
repository
(<https://github.com/samuelb/superproductivity-sync-mcp/security/advisories/new>).
Please do not open a public issue.

Include the release or image tag, how the server is deployed (reverse proxy,
bearer or path token, `MCP_AUTH_DISABLED`), and steps to reproduce. The report
is handled in the private advisory, and details are published once a fixed
release is available.

## Scope

In scope, for example:

- getting past the token check (`TokenAuthMiddleware`) or the DNS-rebinding
  protection;
- leaking the Nextcloud credentials, the encryption password or MCP tokens
  through logs, error messages or tool results;
- a tool call that changes or destroys data in `sync-data.json` beyond what
  the call asked for.

Out of scope:

- deployments with `MCP_AUTH_DISABLED=true` and no authenticating proxy in
  front;
- the path token (`/t/<token>/mcp`) showing up in your reverse proxy's logs,
  which the README documents;
- anything a client with a valid token can do through the documented tools;
  every token acts as the configured Nextcloud user;
- vulnerabilities in Super Productivity or Nextcloud themselves; please
  report those upstream.
