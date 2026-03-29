-- Device Management — Schema v2 (fresh start)
-- Usage: psql -v ON_ERROR_STOP=1 -d bootstrap -f db/schema.sql

CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS citext;

DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='dev') THEN CREATE ROLE dev LOGIN PASSWORD 'dev'; END IF;
EXCEPTION WHEN insufficient_privilege THEN NULL; END $$;

CREATE OR REPLACE FUNCTION set_updated_at() RETURNS TRIGGER AS $$
BEGIN NEW.updated_at = now(); RETURN NEW; END; $$ LANGUAGE plpgsql;

-- ═══════════════════════════════════════════════════════════════
-- CORE : enrollment, connections, relay, queue
-- ═══════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS provisioning (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    email CITEXT NOT NULL,
    device_name TEXT NOT NULL,
    client_uuid UUID NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING'
        CHECK (status IN ('PENDING','ENROLLED','REVOKED','FAILED')),
    encryption_key TEXT NOT NULL,
    comments TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_prov_active ON provisioning(client_uuid) WHERE status IN ('PENDING','ENROLLED');
CREATE INDEX IF NOT EXISTS idx_prov_email ON provisioning(email);
CREATE INDEX IF NOT EXISTS idx_prov_client ON provisioning(client_uuid);

CREATE TABLE IF NOT EXISTS device_connections (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    email CITEXT NOT NULL,
    client_uuid UUID NOT NULL,
    action TEXT NOT NULL DEFAULT 'UNKNOWN',
    encryption_key_fingerprint TEXT NOT NULL,
    connected_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    source_ip INET,
    user_agent TEXT
);
CREATE INDEX IF NOT EXISTS idx_dc_client ON device_connections(client_uuid, connected_at DESC);
CREATE INDEX IF NOT EXISTS idx_dc_email ON device_connections(email, connected_at DESC);

CREATE TABLE IF NOT EXISTS relay_clients (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    client_uuid UUID NOT NULL,
    email CITEXT NOT NULL,
    relay_client_id TEXT NOT NULL,
    relay_key_hash TEXT NOT NULL,
    allowed_targets TEXT[] NOT NULL DEFAULT ARRAY['keycloak']::text[],
    expires_at TIMESTAMPTZ,
    revoked_at TIMESTAMPTZ,
    comments TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_relay_active_client ON relay_clients(client_uuid) WHERE revoked_at IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_relay_active_id ON relay_clients(relay_client_id) WHERE revoked_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_relay_email ON relay_clients(email);

CREATE TABLE IF NOT EXISTS queue_jobs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    topic TEXT NOT NULL,
    payload JSONB NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','processing','done','dead')),
    attempts INT NOT NULL DEFAULT 0,
    max_attempts INT NOT NULL DEFAULT 8,
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    locked_at TIMESTAMPTZ,
    lock_owner TEXT,
    dedupe_key TEXT,
    completed_at TIMESTAMPTZ,
    last_error TEXT
);
CREATE INDEX IF NOT EXISTS idx_qj_poll ON queue_jobs(status, next_attempt_at, created_at);
CREATE UNIQUE INDEX IF NOT EXISTS uq_qj_dedupe ON queue_jobs(topic, dedupe_key);

CREATE TABLE IF NOT EXISTS queue_job_dead_letters (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    job_id UUID NOT NULL,
    topic TEXT NOT NULL,
    payload JSONB NOT NULL,
    dedupe_key TEXT,
    attempts INT NOT NULL,
    max_attempts INT NOT NULL,
    last_error TEXT
);

-- ═══════════════════════════════════════════════════════════════
-- CATALOGUE : plugins, versions, aliases, env overrides
-- ═══════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS plugins (
    id SERIAL PRIMARY KEY,
    slug VARCHAR(100) NOT NULL,
    name VARCHAR(200) NOT NULL,
    description TEXT,
    intent TEXT,
    key_features JSONB DEFAULT '[]'::jsonb,
    changelog JSONB DEFAULT '[]'::jsonb,
    icon_url TEXT,
    icon_path TEXT,
    source_url TEXT,
    device_type VARCHAR(50) NOT NULL,
    category VARCHAR(100) DEFAULT 'productivity',
    homepage_url TEXT,
    support_email TEXT,
    doc_url TEXT,
    license VARCHAR(100),
    publisher VARCHAR(200) DEFAULT 'DNUM',
    visibility VARCHAR(20) DEFAULT 'public'
        CHECK (visibility IN ('public','internal','hidden')),
    status VARCHAR(20) DEFAULT 'active'
        CHECK (status IN ('draft','active','deprecated','removed')),
    maturity VARCHAR(20) DEFAULT 'release'
        CHECK (maturity IN ('dev','alpha','beta','pre-release','release')),
    access_mode VARCHAR(20) DEFAULT 'open'
        CHECK (access_mode IN ('open','waitlist','keycloak_group')),
    required_group VARCHAR(200),
    config_template JSONB,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);
-- Slug must be unique among non-removed plugins (allows re-using a slug after deletion)
CREATE UNIQUE INDEX IF NOT EXISTS uq_plugins_slug_active ON plugins(slug) WHERE status <> 'removed';
CREATE INDEX IF NOT EXISTS idx_plugins_status ON plugins(status);
CREATE INDEX IF NOT EXISTS idx_plugins_device_type ON plugins(device_type);

CREATE TABLE IF NOT EXISTS plugin_aliases (
    alias VARCHAR(100) PRIMARY KEY,
    plugin_id INT NOT NULL REFERENCES plugins(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_pa_plugin ON plugin_aliases(plugin_id);

CREATE TABLE IF NOT EXISTS plugin_env_overrides (
    id SERIAL PRIMARY KEY,
    plugin_id INT NOT NULL REFERENCES plugins(id) ON DELETE CASCADE,
    environment VARCHAR(50) NOT NULL,
    key VARCHAR(200) NOT NULL,
    value TEXT NOT NULL,
    is_secret BOOLEAN DEFAULT false,
    description TEXT,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now(),
    UNIQUE (plugin_id, environment, key)
);
CREATE INDEX IF NOT EXISTS idx_peo ON plugin_env_overrides(plugin_id, environment);

CREATE TABLE IF NOT EXISTS plugin_waitlist (
    id SERIAL PRIMARY KEY,
    plugin_id INT NOT NULL REFERENCES plugins(id) ON DELETE CASCADE,
    email VARCHAR(255) NOT NULL,
    client_uuid VARCHAR(255),
    reason TEXT,
    status VARCHAR(20) DEFAULT 'pending'
        CHECK (status IN ('pending','approved','rejected')),
    reviewed_by VARCHAR(255),
    reviewed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT now(),
    UNIQUE (plugin_id, email)
);
CREATE INDEX IF NOT EXISTS idx_wl ON plugin_waitlist(plugin_id, status);

CREATE TABLE IF NOT EXISTS alias_access_log (
    id BIGSERIAL PRIMARY KEY,
    alias VARCHAR(100) NOT NULL,
    slug VARCHAR(100) NOT NULL,
    plugin_id INT NOT NULL,
    client_uuid VARCHAR(255),
    source_ip INET,
    accessed_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_aal ON alias_access_log(alias, accessed_at DESC);

-- ═══════════════════════════════════════════════════════════════
-- ARTIFACTS, VERSIONS, INSTALLATIONS
-- ═══════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS artifacts (
    id SERIAL PRIMARY KEY,
    device_type VARCHAR(50) NOT NULL,
    platform_variant VARCHAR(50),
    version VARCHAR(50) NOT NULL,
    s3_path TEXT,
    checksum VARCHAR(128),
    min_host_version VARCHAR(50),
    max_host_version VARCHAR(50),
    changelog_url TEXT,
    is_active BOOLEAN DEFAULT true,
    released_at TIMESTAMPTZ DEFAULT now(),
    UNIQUE (device_type, platform_variant, version)
);

CREATE TABLE IF NOT EXISTS plugin_versions (
    id SERIAL PRIMARY KEY,
    plugin_id INT NOT NULL REFERENCES plugins(id) ON DELETE CASCADE,
    artifact_id INT REFERENCES artifacts(id),
    version VARCHAR(50) NOT NULL,
    release_notes TEXT,
    download_url TEXT,
    min_host_version VARCHAR(50),
    max_host_version VARCHAR(50),
    status VARCHAR(20) DEFAULT 'draft'
        CHECK (status IN ('draft','published','deprecated','yanked')),
    distribution_mode VARCHAR(20) DEFAULT 'managed'
        CHECK (distribution_mode IN ('managed','download_link','store','manual')),
    published_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT now(),
    UNIQUE (plugin_id, version)
);
CREATE INDEX IF NOT EXISTS idx_pv_plugin ON plugin_versions(plugin_id);

CREATE TABLE IF NOT EXISTS plugin_installations (
    id SERIAL PRIMARY KEY,
    plugin_id INT NOT NULL REFERENCES plugins(id),
    client_uuid VARCHAR(255) NOT NULL,
    email VARCHAR(255),
    installed_version VARCHAR(50),
    installed_at TIMESTAMPTZ DEFAULT now(),
    last_seen_at TIMESTAMPTZ DEFAULT now(),
    status VARCHAR(20) DEFAULT 'active'
        CHECK (status IN ('active','inactive','uninstalled')),
    UNIQUE (plugin_id, client_uuid)
);
CREATE INDEX IF NOT EXISTS idx_pi_plugin ON plugin_installations(plugin_id);

-- ═══════════════════════════════════════════════════════════════
-- CAMPAGNES, COHORTES, FEATURE FLAGS
-- ═══════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS cohorts (
    id SERIAL PRIMARY KEY,
    name VARCHAR(100) UNIQUE NOT NULL,
    description TEXT,
    type VARCHAR(20) NOT NULL CHECK (type IN ('manual','percentage','email_pattern','keycloak_group')),
    config JSONB,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS cohort_members (
    cohort_id INT NOT NULL REFERENCES cohorts(id) ON DELETE CASCADE,
    identifier_type VARCHAR(20) NOT NULL CHECK (identifier_type IN ('email','client_uuid')),
    identifier_value VARCHAR(255) NOT NULL,
    added_at TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (cohort_id, identifier_type, identifier_value)
);

CREATE TABLE IF NOT EXISTS campaigns (
    id SERIAL PRIMARY KEY,
    name VARCHAR(200) NOT NULL,
    description TEXT,
    type VARCHAR(30) DEFAULT 'plugin_update'
        CHECK (type IN ('plugin_update','config_patch','feature_set')),
    status VARCHAR(20) DEFAULT 'draft'
        CHECK (status IN ('draft','active','paused','completed','rolled_back')),
    environment VARCHAR(50),
    plugin_id INT REFERENCES plugins(id),
    version_id INT REFERENCES plugin_versions(id),
    target_cohort_id INT REFERENCES cohorts(id),
    artifact_id INT REFERENCES artifacts(id),
    rollback_artifact_id INT REFERENCES artifacts(id),
    rollout_config JSONB,
    urgency VARCHAR(10) DEFAULT 'normal' CHECK (urgency IN ('low','normal','critical')),
    deadline_at TIMESTAMPTZ,
    created_by VARCHAR(255),
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_camp_plugin ON campaigns(plugin_id, environment);

CREATE TABLE IF NOT EXISTS campaign_device_status (
    campaign_id INT NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    client_uuid VARCHAR(255) NOT NULL,
    email VARCHAR(255),
    status VARCHAR(20) DEFAULT 'pending'
        CHECK (status IN ('pending','notified','updated','failed','rolled_back')),
    version_before VARCHAR(50),
    version_after VARCHAR(50),
    error_message TEXT,
    last_contact_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (campaign_id, client_uuid)
);
CREATE INDEX IF NOT EXISTS idx_cds ON campaign_device_status(campaign_id, status);

CREATE TABLE IF NOT EXISTS feature_flags (
    id SERIAL PRIMARY KEY,
    name VARCHAR(100) UNIQUE NOT NULL,
    description TEXT,
    default_value BOOLEAN DEFAULT true,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS feature_flag_overrides (
    feature_id INT NOT NULL REFERENCES feature_flags(id) ON DELETE CASCADE,
    cohort_id INT NOT NULL REFERENCES cohorts(id) ON DELETE CASCADE,
    value BOOLEAN NOT NULL,
    min_plugin_version VARCHAR(50),
    PRIMARY KEY (feature_id, cohort_id)
);

-- ═══════════════════════════════════════════════════════════════
-- COMMUNICATIONS, SONDAGES
-- ═══════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS communications (
    id SERIAL PRIMARY KEY,
    type VARCHAR(20) NOT NULL CHECK (type IN ('announcement','alert','survey','changelog')),
    title VARCHAR(300) NOT NULL,
    body TEXT NOT NULL,
    priority VARCHAR(10) DEFAULT 'normal' CHECK (priority IN ('low','normal','high','critical')),
    target_plugin_id INT REFERENCES plugins(id),
    target_cohort_id INT REFERENCES cohorts(id),
    min_plugin_version VARCHAR(50),
    max_plugin_version VARCHAR(50),
    starts_at TIMESTAMPTZ DEFAULT now(),
    expires_at TIMESTAMPTZ,
    survey_question TEXT,
    survey_choices JSONB,
    survey_allow_multiple BOOLEAN DEFAULT false,
    survey_allow_comment BOOLEAN DEFAULT false,
    status VARCHAR(20) DEFAULT 'draft'
        CHECK (status IN ('draft','active','paused','completed','expired')),
    created_by VARCHAR(255),
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_comm_status ON communications(status, starts_at);

CREATE TABLE IF NOT EXISTS survey_responses (
    id SERIAL PRIMARY KEY,
    communication_id INT NOT NULL REFERENCES communications(id) ON DELETE CASCADE,
    client_uuid VARCHAR(255) NOT NULL,
    email VARCHAR(255),
    choices JSONB NOT NULL,
    comment TEXT,
    responded_at TIMESTAMPTZ DEFAULT now(),
    UNIQUE (communication_id, client_uuid)
);

CREATE TABLE IF NOT EXISTS communication_acks (
    communication_id INT NOT NULL REFERENCES communications(id) ON DELETE CASCADE,
    client_uuid VARCHAR(255) NOT NULL,
    acked_at TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (communication_id, client_uuid)
);

-- ═══════════════════════════════════════════════════════════════
-- KEYCLOAK CLIENTS
-- ═══════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS keycloak_clients (
    id SERIAL PRIMARY KEY,
    client_id VARCHAR(200) UNIQUE NOT NULL,
    realm VARCHAR(100) NOT NULL,
    description TEXT,
    client_type VARCHAR(20) DEFAULT 'public' CHECK (client_type IN ('public','confidential')),
    redirect_uris JSONB DEFAULT '[]'::jsonb,
    web_origins JSONB DEFAULT '["*"]'::jsonb,
    pkce_enabled BOOLEAN DEFAULT true,
    direct_access_grants BOOLEAN DEFAULT false,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS plugin_keycloak_clients (
    plugin_id INT NOT NULL REFERENCES plugins(id) ON DELETE CASCADE,
    keycloak_client_id INT NOT NULL REFERENCES keycloak_clients(id) ON DELETE CASCADE,
    environment VARCHAR(50) NOT NULL,
    PRIMARY KEY (plugin_id, keycloak_client_id, environment)
);

-- ═══════════════════════════════════════════════════════════════
-- TELEMETRIE (events extraits)
-- ═══════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS device_telemetry_events (
    id BIGSERIAL PRIMARY KEY,
    client_uuid VARCHAR(255),
    email VARCHAR(255),
    span_name VARCHAR(100),
    span_ts TIMESTAMPTZ,
    attributes JSONB,
    plugin_version VARCHAR(50),
    source_ip INET,
    created_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_dte ON device_telemetry_events(client_uuid, span_ts DESC);

-- ═══════════════════════════════════════════════════════════════
-- AUDIT
-- ═══════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS admin_audit_log (
    id BIGSERIAL PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    actor_email TEXT,
    actor_sub TEXT,
    action TEXT NOT NULL,
    resource_type TEXT,
    resource_id TEXT,
    payload JSONB,
    ip_address INET,
    user_agent TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_at ON admin_audit_log(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_actor ON admin_audit_log(actor_email);

-- ═══════════════════════════════════════════════════════════════
-- ═══════════════════════════════════════════════════════════════
-- GRANTS (dev role)
-- ═══════════════════════════════════════════════════════════════

DO $$ BEGIN
  PERFORM 1 FROM pg_roles WHERE rolname = 'dev';
  IF FOUND THEN
    GRANT CONNECT ON DATABASE bootstrap TO dev;
    GRANT USAGE ON SCHEMA public TO dev;
    GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO dev;
    GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public TO dev;
    ALTER DEFAULT PRIVILEGES IN SCHEMA public
      GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO dev;
    ALTER DEFAULT PRIVILEGES IN SCHEMA public
      GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO dev;
  END IF;
EXCEPTION WHEN insufficient_privilege THEN NULL;
END $$;
