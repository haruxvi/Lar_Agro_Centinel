# AgroVista

Precision agriculture platform for Chilean vineyards.

## Status

Infrastructure scaffold. The backend exposes a modular monolith layout with one
package per bounded context; business endpoints are added as features are
specified.

## Layout

```
backend/app/modules/<context>/   router | service | repository | schema | models
backend/app/shared/              config, db, security, roles, logging, middleware
backend/app/main.py              FastAPI application factory
backend/tests/                   mirrors the module layout
```

## Local development

```bash
python -m venv .venv
./.venv/Scripts/python.exe -m pip install -r requirements-dev.txt
./.venv/Scripts/python.exe -m uvicorn app.main:app --app-dir backend --reload
```

## Quality gates

```bash
./.venv/Scripts/python.exe -m pytest backend/tests/
./.venv/Scripts/python.exe -m ruff check backend/
./.venv/Scripts/python.exe -m mypy backend/app/ --config-file pyproject.toml
./.venv/Scripts/python.exe -m bandit -r backend/ -c .bandit --severity-level high
./.venv/Scripts/python.exe -m pip_audit -r requirements.txt
```
