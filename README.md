# superproductivity-sync-mcp

An [MCP](https://modelcontextprotocol.io) server that lets AI agents (ChatGPT,
Claude, Hermes Agent, Pebble Index, any other MCP client) read and update your
[Super Productivity](https://super-productivity.com) tasks.

It does **not** talk to a running Super Productivity app. Instead it behaves like
one more Super Productivity installation: it reads the shared sync file
(`sync-data.json`) that your devices exchange through Nextcloud, and writes
changes back as entries of the same operation log. Every device picks the
changes up on its next sync, exactly as if you had made them on another device.

## How it works

Super Productivity ≥ 17 syncs through an *operation log*. With the Nextcloud /
WebDAV provider the whole log lives in one file in your sync folder:

```
sync-data.json      pf_2__{ version: 2, syncVersion, vectorClock, state, archiveYoung,
                            archiveOld, recentOps: [...], ... }
sync-data.json.bak  previous content (recovery artifact)
```

* `state` is a full snapshot of the app state written by the last uploader.
* `recentOps` are the last ≤ 2000 operations (compact NgRx actions with vector
  clocks). Clients replay the ones they have not applied yet.

For every write this server:

1. downloads the file (Nextcloud strong `OC-ETag`),
2. applies the change to a copy of the snapshot with a faithful port of the
   app's reducers, and appends the matching operation(s) with its own vector
   clock component,
3. writes the old content to `sync-data.json.bak`, uploads the new file with
   `If-Match`, re-downloads and verifies the hash. On a concurrent write it
   starts over from the fresh remote (up to 3 attempts).

Compressed (`pf_C…`) and encrypted (`pf_…E…`, AES-256-GCM + Argon2id) sync
files are supported; the flags found in the remote file are preserved on write.

## Tools

| Tool | Purpose |
| --- | --- |
| `get_overview`, `get_today`, `get_planner`, `list_tasks`, `get_task` | read tasks |
| `list_projects`, `list_tags`, `list_notes`, `sync_status` | read the rest |
| `search`, `fetch` | generic search/fetch (required by ChatGPT connectors) |
| `create_task`, `update_task`, `schedule_task`, `unschedule_task`, `move_task_to_project`, `delete_task` | tasks |
| `create_project`, `update_project`, `create_tag` | projects & tags |
| `create_note`, `update_note`, `delete_note` | notes |

Days are `YYYY-MM-DD` (or `today`, `tomorrow`, `+N`) in `SP_TIMEZONE`; times are
`HH:MM`; durations are minutes. Not covered on purpose: time tracking,
repeating-task configs, archiving, reminders, reordering and deleting projects
or tags (use the app for those).

## Requirements

* Super Productivity **18.x** with the **Nextcloud** (or WebDAV) sync provider
  and the default single-file format ("Surgical sync" / split files **off**).
  At least one device must have synced once so that `sync-data.json` exists.
* A Nextcloud **app password** for the same account
  (Settings → Security → Devices & sessions).
* Docker with Compose, and a reverse proxy terminating TLS (Traefik, Caddy,
  nginx…). MCP clients require `https://`.

## Deployment

Images are built by GitHub Actions and published to the GitHub Container
Registry as `ghcr.io/samuelb/superproductivity-sync-mcp` (tags: `main` for the latest commit on
`main`, `sha-<commit>`, and `v1.2.3` / `1.2` / `latest` for release tags). The
package inherits the repository's private visibility, so the deployment host
needs a login with a token that has `read:packages`:

```bash
echo "$GHCR_TOKEN" | docker login ghcr.io -u <github-user> --password-stdin
git clone git@github.com:samuelb/superproductivity-sync-mcp.git && cd superproductivity-sync-mcp
cp .env.example .env
$EDITOR .env                      # Nextcloud credentials, sync folder, tokens
openssl rand -hex 32              # -> MCP_AUTH_TOKENS
docker compose pull && docker compose up -d      # IMAGE_TAG=v0.1.0 to pin a release
docker compose logs -f sync-mcp
curl -s http://<container>:8000/healthz   # from inside the proxy network
```

To build locally instead of pulling: `docker compose up -d --build`.

The `sync-mcp` service publishes no host port; attach your proxy to the `proxy`
network and forward to `sync-mcp:8000`. Examples:

* **Traefik**: uncomment the labels in `docker-compose.yml`.
* **Caddy** (bundled, optional): `MCP_PUBLIC_HOST=mcp.example.com docker compose --profile caddy up -d`
  (uses `deploy/Caddyfile`).
* **nginx**: `proxy_pass http://sync-mcp:8000; proxy_buffering off; proxy_read_timeout 600s;`
  plus the usual `Host`/`X-Forwarded-*` headers.

The MCP endpoint is `https://<host>/mcp` (streamable HTTP, stateless, JSON
responses). Health check: `GET /healthz` (unauthenticated).

### Configuration

All settings are environment variables (see `.env.example`).

| Variable | Meaning |
| --- | --- |
| `NEXTCLOUD_URL` | e.g. `https://cloud.example.com` |
| `NEXTCLOUD_USER` | Nextcloud *user id* (as in `/remote.php/dav/files/<user>/`) — the "Username" in Super Productivity's Nextcloud settings |
| `NEXTCLOUD_LOGIN` | optional login name if you sign in with an e-mail address |
| `NEXTCLOUD_PASSWORD` | app password |
| `NEXTCLOUD_SYNC_FOLDER` | sync folder path as configured in the app, e.g. `/super-productivity` |
| `SP_ENCRYPTION_PASSWORD` | only if "Encrypt sync data" is on; when set, a plaintext remote file is refused unless `SP_ALLOW_PLAINTEXT=true` |
| `SP_TIMEZONE` | IANA zone used for "today" (should match your devices) |
| `SP_CLIENT_ID` | optional fixed sync client id; otherwise generated once and stored in the `/data` volume. **Never delete the volume casually** — the id keys this server's vector-clock component |
| `MCP_AUTH_TOKENS` | comma-separated bearer tokens (≥ 16 chars each) |
| `MCP_ALLOW_TOKEN_IN_PATH` | accept `https://host/t/<token>/mcp` for clients without header support (ChatGPT) |
| `MCP_AUTH_DISABLED` | `true` only if your proxy authenticates every request |
| `MCP_ALLOWED_HOSTS` | public host names for DNS-rebinding protection; empty = off (fine behind a proxy with tokens) |
| `SP_CACHE_TTL_SECONDS`, `SP_VERIFY_UPLOAD`, `SP_WRITE_BACKUP`, `SP_MAX_WRITE_ATTEMPTS`, `HTTP_TIMEOUT_SECONDS`, `LOG_LEVEL`, `PORT` | tuning |

Use one token per client so you can revoke them individually.

## Connecting clients

**Claude** (claude.ai custom connector, Claude Desktop, Claude Code):
remote MCP URL `https://mcp.example.com/mcp`, auth: none at the connector level
plus a token in the path if custom headers are not available, or bearer:

```bash
claude mcp add --transport http super-productivity https://mcp.example.com/mcp \
  --header "Authorization: Bearer <token>"
```

**ChatGPT** (Settings → Apps → Advanced → Developer mode → create connector):
ChatGPT supports only OAuth or "No authentication", so set
`MCP_ALLOW_TOKEN_IN_PATH=true` and use the URL `https://mcp.example.com/t/<token>/mcp`
with authentication set to *None*. The connector requires `search` and `fetch`
tools, which this server provides. Treat that URL like a password.

**Hermes Agent** (`~/.hermes/config.yaml`):

```yaml
mcp_servers:
  super_productivity:
    url: "https://mcp.example.com/mcp"
    headers:
      Authorization: "Bearer ${SPMCP_TOKEN}"
```

**Pebble Index** (Index app → MCP & Tool Settings → sandbox group → add MCP
server): URL `https://mcp.example.com/mcp`, header
`Authorization: Bearer <token>`. Pebble Index supports cloud MCP servers with
header authentication only, which is exactly this setup.

Any other client: streamable-HTTP transport, URL `/mcp`, bearer header.

## Development

```bash
uv sync --extra dev
uv run pytest
uv run ruff check src tests && uv run ruff format --check src tests
uv run superproductivity-sync-mcp   # needs a .env
```

Design decisions live in `docs/adr/` — read them before touching the wire
format, the reducer mirror or the security model.

## Safety notes and limitations

* Writes rewrite the whole sync file (as every Super Productivity client does).
  The previous content is kept in `sync-data.json.bak`.
* The snapshot this server reads is as fresh as the last sync of your devices;
  changes made on a device that has not synced yet are not visible.
* If two devices edit the same task concurrently, Super Productivity's
  last-writer-wins conflict handling applies to this server's edits too.
* Operations are stamped with the schema version found in the remote file, so
  all your devices must run a Super Productivity version at least as new as the
  one that last synced (the app enforces this among devices anyway).
* The split-file format ("Surgical sync") and the SuperSync server are not
  supported.
