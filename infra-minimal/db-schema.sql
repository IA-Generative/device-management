-- Schema bootstrap for device management (idempotent).
-- Usage (psql): psql -v ON_ERROR_STOP=1 -d bootstrap -f infra-minimal/db-schema.sql

-- Extensions
CREATE EXTENSION IF NOT EXISTS pgcrypto; -- for gen_random_uuid()
CREATE EXTENSION IF NOT EXISTS citext;   -- case-insensitive email

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

-- Enum for provisioning status
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'provisioning_status') THEN
    CREATE TYPE provisioning_status AS ENUM (
      'PENDING',
      'ENROLLED',
      'REVOKED',
      'FAILED'
    );
  END IF;
END
$$;

-- Trigger function for updated_at
CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
  NEW.updated_at = now();
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- Table: provisioning
CREATE TABLE IF NOT EXISTS provisioning (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  email citext NOT NULL,
  client_uuid UUID NOT NULL,
  status provisioning_status NOT NULL,
  encryption_key text NOT NULL,
  comments text
);

COMMENT ON TABLE provisioning IS 'Enrollment/provisioning records for devices.';
COMMENT ON COLUMN provisioning.email IS 'User email (citext for case-insensitive matching).';
COMMENT ON COLUMN provisioning.client_uuid IS 'Device/client UUID (logical device identity).';
COMMENT ON COLUMN provisioning.status IS 'Provisioning lifecycle status.';
COMMENT ON COLUMN provisioning.encryption_key IS 'Store only safe material (encrypted key/envelope/KMS key id).';

-- updated_at trigger
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_trigger
    WHERE tgname = 'trg_provisioning_set_updated_at'
  ) THEN
    CREATE TRIGGER trg_provisioning_set_updated_at
    BEFORE UPDATE ON provisioning
    FOR EACH ROW
    EXECUTE FUNCTION set_updated_at();
  END IF;
END
$$;

-- Indexes
CREATE INDEX IF NOT EXISTS idx_provisioning_email ON provisioning (email);
CREATE INDEX IF NOT EXISTS idx_provisioning_client_uuid ON provisioning (client_uuid);
CREATE INDEX IF NOT EXISTS idx_provisioning_status ON provisioning (status);
CREATE INDEX IF NOT EXISTS idx_provisioning_updated_at ON provisioning (updated_at DESC);

-- One active provisioning per device (PENDING or ENROLLED)
CREATE UNIQUE INDEX IF NOT EXISTS uq_provisioning_active_client
  ON provisioning (client_uuid)
  WHERE status IN ('PENDING', 'ENROLLED');

-- Optional uniqueness of (email, client_uuid) intentionally NOT enforced
-- to allow historical records and email changes over time.

-- Table: device_connections
CREATE TABLE IF NOT EXISTS device_connections (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  created_at timestamptz NOT NULL DEFAULT now(),
  email citext NOT NULL,
  client_uuid UUID NOT NULL,
  action text NOT NULL DEFAULT 'UNKNOWN',
  encryption_key_fingerprint text NOT NULL,
  connected_at timestamptz NOT NULL DEFAULT now(),
  disconnected_at timestamptz,
  source_ip inet,
  user_agent text
);

COMMENT ON TABLE device_connections IS 'Lightweight audit of device connections.';
COMMENT ON COLUMN device_connections.action IS 'Action/endpoint requested (e.g. ENROLL, CONFIG_GET).';
COMMENT ON COLUMN device_connections.encryption_key_fingerprint IS 'Identifier/fingerprint of the key used (never store raw key).';
COMMENT ON COLUMN device_connections.connected_at IS 'Connection start time (kept separate from created_at for clarity).';
COMMENT ON COLUMN device_connections.source_ip IS 'Client source IP at connection time.';

-- We do NOT add a FK to provisioning.client_uuid because client_uuid is not unique
-- (historical provisioning records are allowed). If you later add a dedicated device table
-- or enforce uniqueness on client_uuid, you can add a FK safely.

-- Indexes for last connections
CREATE INDEX IF NOT EXISTS idx_device_connections_client_connected_at
  ON device_connections (client_uuid, connected_at DESC);

CREATE INDEX IF NOT EXISTS idx_device_connections_email_connected_at
  ON device_connections (email, connected_at DESC);

-- Additional useful indexes
CREATE INDEX IF NOT EXISTS idx_device_connections_client_uuid
  ON device_connections (client_uuid);

-- Grants for dev role (least privilege)
DO $$
BEGIN
  PERFORM 1 FROM pg_roles WHERE rolname = 'dev';
  IF FOUND THEN
    GRANT CONNECT ON DATABASE bootstrap TO dev;
    GRANT USAGE ON SCHEMA public TO dev;
    GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO dev;
    GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public TO dev;
    ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO dev;
    ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO dev;
  END IF;
EXCEPTION
  WHEN insufficient_privilege THEN
    RAISE NOTICE 'Skipping dev grants (insufficient privilege).';
END
$$;

-- Add action column for existing deployments (idempotent)
ALTER TABLE device_connections
  ADD COLUMN IF NOT EXISTS action text NOT NULL DEFAULT 'UNKNOWN';

-- -----------------------------
-- Example queries (safe to copy/paste)
-- -----------------------------

-- 1) Insert a provisioning
-- INSERT INTO provisioning (email, client_uuid, status, encryption_key, comments)
-- VALUES (
--   'user@example.com',
--   gen_random_uuid(),
--   'PENDING',
--   'enc:base64-or-kms-key-id',
--   'Initial enrollment request'
-- );

-- 2) Update status
-- UPDATE provisioning
-- SET status = 'ENROLLED'
-- WHERE client_uuid = '00000000-0000-0000-0000-000000000000'
--   AND status IN ('PENDING', 'FAILED');

-- 3) Get current provisioning for a client_uuid
-- SELECT *
-- FROM provisioning
-- WHERE client_uuid = '00000000-0000-0000-0000-000000000000'
--   AND status IN ('PENDING', 'ENROLLED')
-- ORDER BY updated_at DESC
-- LIMIT 1;

-- 4) Insert a connection
-- INSERT INTO device_connections (
--   email,
--   client_uuid,
--   action,
--   encryption_key_fingerprint,
--   connected_at,
--   source_ip,
--   user_agent
-- ) VALUES (
--   'user@example.com',
--   '00000000-0000-0000-0000-000000000000',
--   'ENROLL',
--   'sha256:abcdef...',
--   now(),
--   '203.0.113.10',
--   'ExampleClient/1.0'
-- );

-- 5) Get last 10 connections for a device
-- SELECT *
-- FROM device_connections
-- WHERE client_uuid = '00000000-0000-0000-0000-000000000000'
-- ORDER BY connected_at DESC
-- LIMIT 10;
