-- Reproduce the AlloyDB privilege split locally.
--
-- Row-level security is not enforced against a table's owner unless the table sets
-- FORCE ROW LEVEL SECURITY, and it is never enforced against a superuser. Running
-- the application as either would make every RLS policy in this project decorative,
-- and the cross-tenant test would pass for the wrong reason. So local Postgres gets
-- the same two roles the deployed database has:
--
--   jobtrack_owner  owns the schema; Alembic migrations run as this role
--   jobtrack_app    the application's role; owns nothing, holds only DML grants
--
-- In AlloyDB, jobtrack_app is an IAM principal and has no password. Locally it needs
-- one, and this file is the password: a fixture committed on purpose, never used
-- outside docker compose.

CREATE ROLE jobtrack_owner WITH LOGIN PASSWORD 'jobtrack' NOCREATEDB NOCREATEROLE NOSUPERUSER;
CREATE ROLE jobtrack_app   WITH LOGIN PASSWORD 'jobtrack' NOCREATEDB NOCREATEROLE NOSUPERUSER;

CREATE DATABASE jobtrack OWNER jobtrack_owner;

-- A second database used only by the test suite, so a test run never destroys a
-- database someone is developing against.
CREATE DATABASE jobtrack_test OWNER jobtrack_owner;

\connect jobtrack

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- The app role may use the schema but may not create in it. Anything it needs is
-- granted explicitly by a migration, so an accidentally unprotected table is a
-- permission error rather than a data leak.
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
ALTER SCHEMA public OWNER TO jobtrack_owner;
GRANT USAGE ON SCHEMA public TO jobtrack_app;

\connect jobtrack_test

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS pg_trgm;

REVOKE CREATE ON SCHEMA public FROM PUBLIC;
ALTER SCHEMA public OWNER TO jobtrack_owner;
GRANT USAGE ON SCHEMA public TO jobtrack_app;
