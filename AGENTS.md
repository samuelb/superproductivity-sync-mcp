# AGENTS.md

Guidance for coding agents working in this repository. The facts below are
verified against the tree; when the code and this file disagree, fix this file
in the same commit.

## Project

`superproductivity-sync-mcp` (Python package `superproductivity_sync_mcp`) is an MCP (Model Context Protocol) server for Super Productivity with
Nextcloud sync enabled. It acts as an additional sync client: it reads and
rewrites `sync-data.json` (the v2 file-based operation log) in the Nextcloud
sync folder via WebDAV. Python 3.14 (`.python-version`), `mcp` SDK 2.x, streamable HTTP.

## Layout

- `src/superproductivity_sync_mcp/config.py` — settings from env/.env (`Settings`).
- `src/superproductivity_sync_mcp/webdav.py` — Nextcloud WebDAV client (GET/PUT with `If-Match`, PROPFIND).
- `src/superproductivity_sync_mcp/codec.py` — `pf_[C][E]2__` prefix, gzip+base64, AES-GCM/Argon2id.
- `src/superproductivity_sync_mcp/syncfile.py` — envelope model, vector clocks, constants.
- `src/superproductivity_sync_mcp/ops.py` — compact operation records and the action-code registry.
- `src/superproductivity_sync_mcp/reducers.py` — port of the app's reducers applied to the snapshot.
- `src/superproductivity_sync_mcp/mutations.py` — tool-level changes: validate, reduce, emit op.
- `src/superproductivity_sync_mcp/store.py` — download → mutate → conditional upload with retry.
- `src/superproductivity_sync_mcp/queries.py` — read-only views. `server.py` — MCP tools. `app.py` — ASGI app + auth.
- `src/superproductivity_sync_mcp/ids.py` — nanoid / UUIDv7 / client ids. `timeutil.py` — "today", start-of-next-day, day parsing.
- `src/superproductivity_sync_mcp/__main__.py` — entry point: settings, start-up probe, uvicorn.
- `tests/` — pytest; `tests/fake_dav.py` is an in-process Nextcloud stand-in.
- `docs/adr/` — decisions (index: `docs/adr/README.md`).

## Commands

- `uv sync --extra dev` — install. `uv run pytest` — tests. `uv run ruff check src tests && uv run ruff format --check src tests` — lint.
- `uv run superproductivity-sync-mcp` — run locally (needs `.env`). `docker compose pull && docker compose up -d` — deploy the published image; `--build` builds locally.
- CI (`.github/workflows/ci.yml`) runs lint+tests and publishes `ghcr.io/samuelb/superproductivity-sync-mcp` (see ADR-0009). Keep `uv.lock` current (`--frozen`). Dependabot (`.github/dependabot.yml`) opens weekly PRs for actions, the Docker base image and Python deps; merge them after CI passes.

## Rules

- Every state-changing tool must (a) emit an operation whose code and payload
  match the app's action creator and (b) apply the identical change to the
  snapshot via `reducers.py`, with a test in `tests/test_reducers.py`.
  Verify against the Super Productivity source of the targeted version
  (currently 18.21.x) — do not guess reducer behaviour.
- Op payloads must carry entities as they were *before* the reducer ran (what
  the app dispatches). The reducer mirror mutates state entities in place, so
  `copy.deepcopy` any entity you put into a payload before calling `reducers.*`.
- Never write `None` where the app writes `undefined`; use `reducers.UNSET`.
- Mutations may be re-run on write conflicts: keep them pure functions of `ctx`.
- Do not add tools that emit full-state operations or touch archives.

## Workflow

- Use trunk-based development and commit directly to the `main` branch. Create
  feature branches only when necessary.
- Follow the Conventional Commits specification.
- Decisions live in `docs/adr/` (index: `docs/adr/README.md`). Read the
  relevant records before changing the architecture, wire protocol, security
  model, or release process. When a change makes a decision that future work
  must respect, or reverses an existing one, add a record based on
  `docs/adr/0000-template.md` or mark the old record as superseded in the same
  commit. Rejected ideas get a record too.
