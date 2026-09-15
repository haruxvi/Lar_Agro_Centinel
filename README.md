# Lar Agro Centinel

> *Lar*: in Roman tradition, the guardian spirit of the household and the land.
> This system watches over what happens on the farm — the crops, the animals,
> the tools, and the people.

Agricultural operations platform for Chilean farms. Combines satellite and drone
imagery analysis with traceability of livestock, inputs, assets and personnel,
under a role-segmented permission model where every action is recorded.

---

## Why

Chilean agriculture runs under real regulatory obligations — SAG traceability for
livestock and phytosanitary products, Law 21.719 for personal data — but most
field operations are still tracked on paper, WhatsApp and spreadsheets. When an
inspection comes, or a product batch has to be traced back, the record does not
exist.

Lar Agro Centinel treats traceability as a first-class feature rather than a
reporting afterthought: every operation is attributed to a person, a role, a
plot and a timestamp, in an append-only audit trail.

---

## Scope

| Domain | What it covers |
|---|---|
| **Crops** | Satellite NDVI (Sentinel-2), drone RGB indices, anomaly detection, historical trends |
| **Livestock** | Identification and movement traceability (SAG / SIPEC alignment) |
| **Inputs** | Warehouse stock, batch tracking, expiry control, phytosanitary product registry |
| **Assets** | Drone and equipment registry, operational history, maintenance state |
| **People** | Task assignment, PPE declaration, re-entry intervals, full action attribution |

---

## Design principles

**No superadministrator.** Nine roles with granular permissions and segregation
of duties. The person who plans an operation is not the person who executes it.
The person who recommends a treatment is not the person who approves it. Not
even the owner can alter the audit log.

**Audit by design.** Every significant action writes an append-only record:
who, what, when, where, under which role, with what outcome. Records are
hash-chained so tampering is detectable.

**Responsible Operation Mode.** Configurable policies that constrain field
operations to protect pollinators and workers — minimum flight altitude,
exclusion zones over registered apiaries, flowering-calendar validation,
advance notice to nearby beekeepers, mandatory PPE checks, re-entry intervals.
Based on peer-reviewed apicultural literature and SAG regulation, not folklore.

**Security is not a later phase.** Fail-closed authentication, mandatory 2FA for
privileged roles, strict multitenancy isolation, SAST in CI, dependency auditing
on every push.

---

## Roles

| Role | Can | Cannot |
|---|---|---|
| Propietario | See everything on own plots, assign users, set policies | Alter logs, operate equipment |
| Admin Operaciones | Plan missions, assign tasks, approve applications | Change roles, execute field work |
| Agrónomo | Analyse, report, recommend | Execute or approve operations |
| Operador de Drone | Execute missions, upload captures | Plan missions, access other plots |
| Aplicador | Execute applications, declare PPE | Approve or plan applications |
| Bodeguero | Register stock in/out | Approve restricted movements |
| Jefe de Bodega | Approve movements, manage layout | Alter past records |
| Auditor | Read everything, export logs | Write anything, ever |
| Apicultor | Receive proximity notifications | Access farm data |

Roles are cumulative per user and scoped per plot. The active role is recorded
with every action.

---

## Stack

**Backend** — Python, FastAPI, SQLAlchemy 2, PostgreSQL + PostGIS, Redis,
structlog, PyJWT, pyotp

**Geospatial & analysis** — rasterio, GeoPandas, Shapely, sentinelhub-py,
OpenCV, scikit-learn

**Frontend** — React, Vite, TypeScript, TailwindCSS, shadcn/ui, TanStack Query,
Zustand, Leaflet, Recharts

**Quality** — pytest, ruff, mypy (strict), Bandit, pip-audit, CodeQL

---

## Architecture

Modular monolith with layered architecture. One package per bounded context,
each exposing a clear public API; modules communicate through an internal event
bus rather than direct calls. This keeps extraction into separate services a
mechanical change if scale ever requires it, without paying distributed-system
costs today.

---

## Copyright and usage

Copyright (c) 2026 haruxvi. All rights reserved.

This is proprietary software. The source is public for portfolio and
transparency purposes only — no license is granted to use, copy,
modify or distribute it. See the [NOTICE](NOTICE) file for details.

For licensing or commercial inquiries, open an issue.
