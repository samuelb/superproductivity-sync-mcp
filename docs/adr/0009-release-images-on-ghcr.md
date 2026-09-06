# ADR-0009: Build and publish container images with GitHub Actions to GHCR

- **Status:** Accepted
- **Date:** 2026-09-07
- **Sources:** `.github/workflows/ci.yml`, `docker-compose.yml`, `README.md`

## Context

The deployment host should run a tested, reproducible image rather than build
from source. The code lives in a private GitHub repository, which offers a
container registry with the same access control.

## Decision

- One workflow (`CI`): a `test` job (ruff, pytest via uv, frozen lockfile) and
  an `image` job that runs only after tests pass and only for pushes (never for
  pull requests from forks).
- Images go to `ghcr.io/samuelb/superproductivity-sync-mcp`, multi-arch (`linux/amd64`,
  `linux/arm64`), authenticated with the workflow's `GITHUB_TOKEN`.
- Tags: `main` (moving), `sha-<short sha>` (immutable), and for git tags
  `v<semver>`: `<version>`, `<major>.<minor>`, `latest`. A release is made by
  pushing a `v*` tag.
- `docker-compose.yml` defaults to `ghcr.io/samuelb/superproductivity-sync-mcp:${SPMCP_TAG:-main}`
  and keeps `build: .` for local builds.

## Consequences

- The package is private like the repo; pulling requires `docker login ghcr.io`
  with a `read:packages` token on the deployment host.
- `uv.lock` must be committed and current (`--frozen`), otherwise CI fails.
- Build cache lives in GitHub Actions cache; first multi-arch builds are slow.
- Dependabot keeps actions, the base image and the uv lockfile current via
  weekly grouped PRs (`.github/dependabot.yml`); the `test` job gates them.

## Rejected alternatives

- Docker Hub: a second account and secret to manage for no benefit.
- Building on the deployment host: no test gate and non-reproducible.
