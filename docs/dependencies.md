# Dependency decisions — Phase 0

This document records every version that differs from the Phase 0 setup prompt and
why. The governing rules, in order of precedence, were:

1. Zero known vulnerabilities (`pip-audit`, `npm audit`) wins over exact pins.
2. A version that installs and works together with the rest of the stack wins over
   an incompatible pin.
3. Otherwise the pin from the prompt is kept.

The Python runtime itself is covered by
[ADR-001](decisions/ADR-001-python-version.md).

## Backend (Python 3.12)

| Package | Prompt pin | Final | Reason |
|---|---|---|---|
| Pillow | 12.2.0 | 12.3.0 | 13 advisories affecting 12.2.0; all fixed in 12.3.0. |
| transformers | 4.46.2 | 5.17.0 | 26 advisories on 4.46.2, several with no fix in the 4.x line; the first clean release is 5.10.0. Latest stable used. Major version bump; no code depends on it yet. |
| python-multipart | 0.0.20 | 0.0.32 | 6 advisories; the last one is fixed in 0.0.31. |
| cryptography | 43.0.3 | 50.0.1 | 6 advisories; the last one is fixed in 49.0.0. |
| pytest | 8.3.4 | 9.1.1 | PYSEC-2026-1845, fixed in 9.0.3. |
| pytest-cov | 6.0.0 | 7.1.0 | Aligned with pytest 9 (founder decision). |
| email-validator | 2.2.0 | 2.3.0 | Founder decision. |
| pytest-asyncio | 0.24.0 | 1.4.0 | 0.24.0 requires `pytest<9`, incompatible with the patched pytest. |
| redis | 8.1.0 (pre-existing) | 5.3.1 | Every arq release, including the latest (0.28.0), requires `redis<6`. 5.3.1 is the latest 5.x and has no known advisories. |
| bcrypt (transitive) | — | 4.3.0 | passlib 1.7.4 fails with bcrypt 5.0.0 (`ValueError: password cannot be longer than 72 bytes` during its backend self-test). Verified that hashing and verification work with 4.3.0. |

All other runtime and development pins match the prompt. Pre-existing tooling pins
(`mypy 2.3.1`, `ruff`, `bandit`, `pip-audit`) are unchanged.

Verification on the final set: `pip check` reports no broken requirements and
`pip-audit -r requirements.txt` reports no known vulnerabilities.

### Follow-ups worth a decision

- **passlib is unmaintained** (last release 2020) and is the reason bcrypt must stay
  on 4.x. Phase 1 (auth) is the natural moment to decide whether to keep it or move to
  a maintained alternative.
- **opencv-python** stays on the 4.10 line as pinned; the latest release is 5.0,
  a major version change to evaluate when computer-vision code is written.

## Frontend

| Item | Prompt | Final | Reason |
|---|---|---|---|
| React | React 18 | 18.3.1 | `npm create vite@latest` scaffolds React 19, but `react-leaflet@4` requires React 18. |
| react-router-dom | `@6` | 7.18.3 | Every 6.x release is affected by GHSA-337j-9hxr-rhxg (moderate); the fix is only available in 7.18.3. The APIs in use (`createBrowserRouter`, `RouterProvider`) are unchanged. |
| Vite | Vite 5 | 8.3.0 | Scaffolded by `create-vite` 9.2.1; the prompt pins no Vite version. |
| TypeScript | TypeScript 5 | 6.0.3 | Scaffolded by `create-vite`; the prompt pins no TypeScript version. |
| Linter | ESLint 9 | ESLint 9 | The template ships oxlint; it was removed and replaced with ESLint 9 (flat config, `typescript-eslint` strict + stylistic type-checked, React, React Hooks, Prettier). |
| shadcn/ui CLI | `shadcn@latest` | `shadcn@2.3.0` | Current shadcn CLI targets Tailwind v4, while the prompt installs `tailwindcss@3`. 2.3.0 is the last CLI for Tailwind v3. Its non-interactive mode cannot select style or base color, so `components.json` (new-york, slate, CSS variables, no RSC) was written first and `init --defaults --force` reused it. |
| Node.js (Docker, CI) | 20 | 24 LTS | Node 20 reached end of life on 2026-04-30, and Vite 8 requires `^20.19 \|\| >=22.12`. |
| Type check in CI | `npx tsc --noEmit` | `npm run type-check` (`tsc -b`) | The root `tsconfig.json` only holds project references, so `tsc --noEmit` checks no files. `tsc -b` type-checks every referenced project. |

Generated shadcn components received two lint fixes to pass the strict ESLint
configuration: a redundant `String(...)` conversion removed in `form.tsx`, and the
custom `cmdk-input-wrapper` attribute allow-listed for `react/no-unknown-property`.

Verification: `npm audit` reports 0 vulnerabilities; `npm run lint`,
`npm run type-check` and `npm run build` pass.

## Infrastructure and CI

| Item | Prompt | Final | Reason |
|---|---|---|---|
| `actions/checkout` | v4 | v7 | Latest major. |
| `actions/setup-python` | v5 | v7 | Latest major. |
| `actions/setup-node` | v4 | v7 | Latest major. |
| `github/codeql-action` | v3 | v4 | Latest major; v3 is scheduled for deprecation. |
| CodeQL permissions | `security-events: write` | + `actions: read`, `contents: read` | A job-level `permissions` block drops every unlisted scope, so checkout of a private repository would fail without `contents: read`. |
| CodeQL language | `javascript` | `javascript-typescript` | Current language identifier; also analyzes TypeScript. |
| Workflow permissions | none | `contents: read` | Least privilege for the quality workflows. |
| Vite dev proxy | `http://localhost:8000` | `VITE_PROXY_TARGET` or `http://localhost:8000` | Inside Docker Compose the proxy runs in the frontend container, where `localhost` is not the backend. Compose sets `VITE_PROXY_TARGET=http://backend:8000`. |
| `Dockerfile.backend` | as prompt | + `--no-install-recommends`, non-root user, Python env flags | Smaller image and no root process. Inline comment moved off the `FROM` line, where Docker does not allow it. |
| `.gitattributes` | — | `* text=auto eol=lf` | Files mounted into Linux containers must not get CRLF line endings on Windows checkouts. |
| Postgres healthcheck | `pg_isready -U lar_dev` | `pg_isready -h 127.0.0.1 -U lar_dev -d lar_agro_centinel` | On first start the PostGIS entrypoint runs a temporary server listening only on the Unix socket while it creates extensions. The socket-based check reported healthy ~15 s early and the backend got `Connection refused`. Forcing TCP waits for the real server. |
| Backend reload in Docker | `--reload` | `--reload --reload-dir backend` + `WATCHFILES_FORCE_POLLING=true` | inotify does not work on Windows bind mounts: the reloader crashed with `WatchfilesRustInternalError: Cannot allocate memory (os error 12)`, disabling hot reload. |
| Frontend reload in Docker | — | `server.watch.usePolling` driven by `VITE_USE_POLLING` (set in Compose) | Same bind-mount limitation for Vite's file watcher. Local runs keep native file events. |

### Heads-up

- **CodeQL on a private repository** requires GitHub Code Security (Advanced
  Security) to be enabled for the repository. Without it, `codeql.yml` fails at the
  upload step.
- The backend image installs `gdal-bin`, `libgdal-dev` and `build-essential` as the
  prompt requests. Per ADR-001 every geospatial package installs from wheels that
  bundle GDAL, so these system packages are likely unnecessary and could be removed
  to shrink the image and speed up builds.
