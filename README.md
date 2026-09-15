# Lar Agro Centinel

System inspired by the figure of the Lar as a rural guardian, designed for the
monitoring and protection of crops, livestock, agricultural infrastructure, and
workers.

## Status

Phase 0 — technical setup. Infrastructure, module layout and quality gates are in
place; business features are implemented in later phases.

## Stack

| Layer | Technology |
|---|---|
| Backend | Python 3.12, FastAPI, SQLAlchemy 2, Alembic, pydantic-settings, structlog |
| Frontend | React 18, TypeScript, Vite, Tailwind CSS 3, shadcn/ui, TanStack Query, Zustand |
| Data | PostgreSQL 16 + PostGIS, Redis 7 |
| Tooling | ruff, mypy (strict), bandit, pip-audit, pytest, ESLint 9, Prettier |

Version decisions: [ADR-001 (Python)](docs/decisions/ADR-001-python-version.md) and
[dependency decisions](docs/dependencies.md).

## Layout

```
backend/app/modules/<context>/   router | service | repository | schema | models
backend/app/shared/              config, db, security, roles, enums, permissions, events, ...
backend/app/main.py              FastAPI application factory
backend/tests/                   mirrors the module layout (+ integration/)
frontend/src/                    components, pages, services, store, types, config, ...
scripts/init-db.sql              PostgreSQL extensions for the local database
docs/                            architecture decisions and dependency notes
```

Bounded contexts: `auth`, `users`, `predios`, `devices`, `captures`, `analysis`,
`warehouses`, `inventory`, `products`, `operations`, `responsible_mode`, `audit`,
`notifications`, `beekeepers`, `health`.

## Configuration

Copy the template and replace both secrets with random values of at least 32
characters. `.env` is git-ignored and must never be committed.

```bash
cp .env.example .env
```

## Run with Docker Compose

```bash
docker compose up -d --build
```

| Service | URL |
|---|---|
| Backend | http://localhost:8000 (`/health`, `/docs`) |
| Frontend | http://localhost:5173 |
| PostgreSQL | localhost:5432 |
| Redis | localhost:6379 |

## Run locally

Backend (Python 3.12, see ADR-001):

```bash
uv python install 3.12
uv venv --python 3.12 .venv
./.venv/Scripts/python.exe -m pip install -r requirements-dev.txt
./.venv/Scripts/python.exe -m uvicorn app.main:app --app-dir backend --reload
```

Frontend:

```bash
cd frontend
npm install
npm run dev
```

## Quality gates

Backend:

```bash
./.venv/Scripts/python.exe -m pytest backend/tests/
./.venv/Scripts/python.exe -m ruff check backend/
./.venv/Scripts/python.exe -m mypy backend/app/ --config-file pyproject.toml
./.venv/Scripts/python.exe -m bandit -r backend/ -c .bandit --severity-level high
./.venv/Scripts/python.exe -m pip_audit -r requirements.txt
```

Frontend (from `frontend/`):

```bash
npm run lint
npm run type-check
npm run format:check
npm run build
```

The same gates run in GitHub Actions (`.github/workflows/`), plus a CodeQL scan.
