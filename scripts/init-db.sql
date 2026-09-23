-- Runs once, when the PostgreSQL data directory is first created.
-- Executed by the bootstrap superuser (POSTGRES_USER = lar_owner).

-- PostGIS and utility extensions
CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS postgis_topology;
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- Runtime role. The application connects with this role, never with the schema
-- owner: see docs/decisions/ADR-002-audit-log-immutability.md.
-- Table-level privileges are granted by the Alembic migrations.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'lar_app') THEN
        CREATE ROLE lar_app WITH LOGIN PASSWORD 'lar_app_password';
    END IF;
END
$$;

-- Grant on whichever database this script runs against (dev or test).
DO $$
BEGIN
    EXECUTE format('GRANT CONNECT ON DATABASE %I TO lar_app', current_database());
END
$$;

GRANT USAGE ON SCHEMA public TO lar_app;

-- Ready to accept alembic migrations
