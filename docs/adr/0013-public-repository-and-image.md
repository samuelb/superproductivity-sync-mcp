# ADR-0013: Publish the repository and the container image

- **Status:** Accepted (supersedes the private visibility of ADR-0009)
- **Date:** 2026-09-29
- **Sources:** `LICENSE`, `THIRD_PARTY_NOTICES.md`, `SECURITY.md`, `README.md`,
  `.github/workflows/ci.yml`, `docker-compose.yml`

## Context

ADR-0009 kept the repository and the GHCR package private, so every
deployment host needed a `read:packages` token and nobody else could use the
server. The project is useful to anyone who syncs Super Productivity through
Nextcloud, and its code is partly a port of MIT-licensed Super Productivity
code, which may be redistributed with its notice.

## Decision

- The GitHub repository and the `ghcr.io/samuelb/superproductivity-sync-mcp`
  package are public; pulling the image needs no login.
- The project is MIT-licensed (`LICENSE`). Super Productivity's MIT notice is
  kept in `THIRD_PARTY_NOTICES.md`, and both ship in the wheel and the image.
- Vulnerabilities are reported through GitHub's private vulnerability
  reporting (`SECURITY.md`), not public issues.
- Secret scanning with push protection is enabled (2026-09-30): a push that
  contains a recognised credential is rejected before it becomes public.
- Third-party actions in CI are pinned to full commit SHAs with the release
  in a comment; Dependabot updates both.
- Build, tags and the `docker-compose.yml` default from ADR-0009 are
  unchanged.

## Consequences

- A public GHCR package cannot be made private again.
- Everything committed is world-readable, including history: fixtures and
  docs use `example.com` hosts and placeholder credentials only. Real sync
  files and `.env` belong in the git-ignored `testdata/`, `data/` and `.env`.
- Pull requests from forks run the test job with a read-only token; the
  image job never runs for pull requests.
- Changes to the ported reducers must keep the upstream notice accurate.

## Rejected alternatives

- **Stay private and grant access per person.** Every user would need a
  GitHub account, a collaborator invite and a `read:packages` token.
