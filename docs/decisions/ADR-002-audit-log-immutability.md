# ADR-002: audit_log immutability

- **Status:** Accepted
- **Date:** 2026-09-23
- **Deciders:** haruxvi

## Context

`audit_log` is the system's record of who did what, when, under which role and
with what outcome. Traceability obligations — SAG registries for phytosanitary
products and livestock, and Ley 21.719 for personal data — assume that record is
trustworthy. An audit trail that the application itself can rewrite proves
nothing: if the runtime can edit history, so can anybody who takes over the
runtime.

The Phase 1 plan called for `REVOKE UPDATE, DELETE ON audit_log FROM PUBLIC`.

## Finding

That revoke does not protect the table. In PostgreSQL the table owner keeps
every privilege regardless of grants, and the application was connecting as the
owner. Measured on the running database:

```
INSERT 0 1   UPDATE 1   DELETE 1        -- executed as the owner
has_table_privilege('public',    'audit_log', 'UPDATE') = false
has_table_privilege('lar_owner', 'audit_log', 'UPDATE') = true
```

The rows could be rewritten and erased at will.

## Decision

Defence in depth, both layers applied now.

**Layer 1 — a trigger inside the database.** `audit_log_prevent_mutation()`
raises `insufficient_privilege` from `BEFORE UPDATE`, `BEFORE DELETE` and
`BEFORE TRUNCATE` triggers. This binds every role, the owner included. The
TRUNCATE trigger is statement-level because `TRUNCATE` never fires row-level
triggers.

**Layer 2 — least privilege.** Two database roles:

| Role | Used by | Privileges |
|---|---|---|
| `lar_owner` | Alembic migrations only | Owns the schema |
| `lar_app` | Application runtime | `INSERT`, `SELECT` on `audit_log`; full CRUD on the rest |

`ALTER DEFAULT PRIVILEGES FOR ROLE lar_owner` grants `lar_app` access to tables
created by future migrations, so later phases do not fail with "permission
denied" until someone remembers a manual grant. `audit_log` is then explicitly
revoked again, because those default privileges would otherwise hand it back.

The split is introduced now rather than before production: every later phase
would otherwise be written assuming an owner connection, and migrating that
assumption later is riskier than paying for it today.

## Alternatives considered

1. **`REVOKE` alone.** Rejected: measured ineffective against the owner, which
   is exactly the role the application used.
2. **Trigger alone.** Rejected: blocks mutation but leaves the runtime holding
   far more privilege than it needs, so any other defect is amplified.
3. **Separate role alone.** Rejected: does not constrain the owner, and
   migrations plus any operator session run as the owner.

## Known limit

The owner can `DROP TRIGGER` and then mutate the table. Prevention has a
ceiling here: whoever owns the schema can undo the controls that live in the
schema.

That gap is covered in Phase 7 by the hash chain, which makes tampering
**detectable** even when it is not preventable. Prevention and detection are
different controls and this system needs both: the trigger and the role split
stop the accidental and the opportunistic case, the hash chain catches the
deliberate one.

## Consequences

- Two database roles and **two connection URLs**: `DATABASE_URL` for the runtime
  and `DATABASE_MIGRATION_URL` for Alembic. Both must be configured in
  `.env`, `docker-compose.yml` and CI.
- `scripts/init-db.sql` creates `lar_app`. It only runs when the PostgreSQL data
  directory is created, so an existing volume needs `docker compose down -v` or
  the script applied by hand.
- **Every future migration that creates tables inherits the default privileges**
  — and any table that must not be mutable needs its own explicit revoke, the
  way `audit_log` does.
- The test suite connects with both roles to prove each layer independently.
