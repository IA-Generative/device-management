-- Migration 003: admin audit log + device telemetry events
-- Idempotent (uses CREATE TABLE IF NOT EXISTS, CREATE INDEX IF NOT EXISTS)

-- Admin audit trail (INSERT only — never UPDATE or DELETE)
CREATE TABLE IF NOT EXISTS admin_audit_log (
    id           BIGSERIAL PRIMARY KEY,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    actor_email  TEXT NOT NULL,
    actor_sub    TEXT NOT NULL,
    action       TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id  TEXT,
    payload      JSONB,
    ip_address   INET,
    user_agent   TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_created_at ON admin_audit_log (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_actor ON admin_audit_log (actor_email);
CREATE INDEX IF NOT EXISTS idx_audit_resource ON admin_audit_log (resource_type, resource_id);

-- Device telemetry events (circular buffer, max 200 per device via trigger)
CREATE TABLE IF NOT EXISTS device_telemetry_events (
    id             BIGSERIAL PRIMARY KEY,
    received_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    client_uuid    TEXT NOT NULL,
    span_name      TEXT NOT NULL,
    span_ts        TIMESTAMPTZ,
    attributes     JSONB,
    platform       TEXT,
    plugin_version TEXT
);
CREATE INDEX IF NOT EXISTS idx_telemetry_events_uuid
    ON device_telemetry_events (client_uuid, received_at DESC);

-- Trigger: keep only the 200 most recent events per device
CREATE OR REPLACE FUNCTION trim_telemetry_events() RETURNS trigger AS $$
BEGIN
    DELETE FROM device_telemetry_events
    WHERE client_uuid = NEW.client_uuid
      AND id NOT IN (
          SELECT id FROM device_telemetry_events
          WHERE client_uuid = NEW.client_uuid
          ORDER BY received_at DESC LIMIT 200
      );
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_trim_telemetry_events ON device_telemetry_events;
CREATE TRIGGER trg_trim_telemetry_events
AFTER INSERT ON device_telemetry_events
FOR EACH ROW EXECUTE FUNCTION trim_telemetry_events();
