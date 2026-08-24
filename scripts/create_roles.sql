-- Role setup for local development / CI, mirroring §10 "Security implications":
--
--   - The application connects as a role that is NOT the table owner and
--     does NOT have BYPASSRLS.
--   - Migrations run as a separate owner role, out-of-band, never with
--     request-serving credentials.
--   - The background worker connects as yet another distinct role,
--     narrowly granted a §10.3-rule-10 exception on `jobs` alone by the
--     migration itself (see app/db/rls.py grant_worker_jobs_exception) —
--     this script only needs to create the role; the migration grants it.
--
-- Run this ONCE against a fresh database, as a superuser, before running
-- Alembic. In production, provision these via your cloud provider's IAM/
-- role management rather than a hand-run script — this file is for local
-- Docker Compose / CI convenience only. CHANGE EVERY PASSWORD BELOW.

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'migrator') THEN
        CREATE ROLE migrator LOGIN PASSWORD 'migrator_password' CREATEROLE;
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_user') THEN
        CREATE ROLE app_user LOGIN PASSWORD 'app_password' NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;
    END IF;

    -- worker_role is created idempotently by the migration itself
    -- (app/db/rls.py grant_worker_jobs_exception) the first time it runs,
    -- with a placeholder password you MUST rotate before production.
END
$$;

-- The database itself should be owned by `migrator` so that `migrator`
-- can create/alter tables, and `app_user` / `worker_role` never can:
-- ALTER DATABASE aiagentv8 OWNER TO migrator;

GRANT CONNECT ON DATABASE aiagentv8 TO app_user, migrator;

-- Table-level grants for app_user/worker_role are applied by the
-- migration after it creates the schema (it knows the exact table list;
-- this script runs before any table exists).
