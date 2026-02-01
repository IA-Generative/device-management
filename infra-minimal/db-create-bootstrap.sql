-- Idempotent database creation script for "bootstrap".
-- Note: CREATE DATABASE cannot run inside a transaction block.
-- Usage (psql): psql -v ON_ERROR_STOP=1 -f infra-minimal/db-create-bootstrap.sql

SELECT 'CREATE DATABASE bootstrap'
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'bootstrap')\gexec

-- Ensure dev role exists for local/dev usage (best-effort).
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'dev') THEN
    CREATE ROLE dev LOGIN PASSWORD 'dev' NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
  END IF;
EXCEPTION
  WHEN insufficient_privilege THEN
    RAISE NOTICE 'Skipping role creation (insufficient privilege).';
END
$$;
