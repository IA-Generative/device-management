-- Migration 002: campaigns, cohorts, feature flags, artifacts
-- Idempotent (uses CREATE TABLE IF NOT EXISTS, CREATE INDEX IF NOT EXISTS)

-- cohorts
CREATE TABLE IF NOT EXISTS cohorts (
  id          SERIAL PRIMARY KEY,
  name        VARCHAR(100) UNIQUE NOT NULL,
  description TEXT,
  type        VARCHAR(20) NOT NULL CHECK (type IN ('manual','percentage','email_pattern','keycloak_group')),
  config      JSONB NOT NULL DEFAULT '{}',
  created_at  TIMESTAMPTZ DEFAULT NOW(),
  updated_at  TIMESTAMPTZ DEFAULT NOW()
);

-- cohort_members
CREATE TABLE IF NOT EXISTS cohort_members (
  cohort_id        INTEGER REFERENCES cohorts(id) ON DELETE CASCADE,
  identifier_type  VARCHAR(20) NOT NULL CHECK (identifier_type IN ('email','client_uuid')),
  identifier_value VARCHAR(255) NOT NULL,
  added_at         TIMESTAMPTZ DEFAULT NOW(),
  PRIMARY KEY (cohort_id, identifier_type, identifier_value)
);

-- feature_flags
CREATE TABLE IF NOT EXISTS feature_flags (
  id            SERIAL PRIMARY KEY,
  name          VARCHAR(100) UNIQUE NOT NULL,
  description   TEXT,
  default_value BOOLEAN NOT NULL DEFAULT TRUE,
  created_at    TIMESTAMPTZ DEFAULT NOW(),
  updated_at    TIMESTAMPTZ DEFAULT NOW()
);

-- feature_flag_overrides
CREATE TABLE IF NOT EXISTS feature_flag_overrides (
  feature_id          INTEGER REFERENCES feature_flags(id) ON DELETE CASCADE,
  cohort_id           INTEGER REFERENCES cohorts(id) ON DELETE CASCADE,
  value               BOOLEAN NOT NULL,
  min_plugin_version  VARCHAR(50),
  updated_at          TIMESTAMPTZ DEFAULT NOW(),
  PRIMARY KEY (feature_id, cohort_id)
);

-- artifacts
CREATE TABLE IF NOT EXISTS artifacts (
  id               SERIAL PRIMARY KEY,
  device_type      VARCHAR(50) NOT NULL,
  platform_variant VARCHAR(50),
  version          VARCHAR(50) NOT NULL,
  s3_path          TEXT NOT NULL,
  checksum         VARCHAR(128) NOT NULL,
  min_host_version VARCHAR(50),
  max_host_version VARCHAR(50),
  changelog_url    TEXT,
  is_active        BOOLEAN DEFAULT TRUE,
  released_at      TIMESTAMPTZ DEFAULT NOW(),
  UNIQUE (device_type, platform_variant, version)
);

-- campaigns
CREATE TABLE IF NOT EXISTS campaigns (
  id                   SERIAL PRIMARY KEY,
  name                 VARCHAR(200) NOT NULL,
  description          TEXT,
  type                 VARCHAR(30) NOT NULL CHECK (type IN ('plugin_update','config_patch','feature_set')),
  status               VARCHAR(20) NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','active','paused','completed','rolled_back')),
  target_cohort_id     INTEGER REFERENCES cohorts(id),
  artifact_id          INTEGER REFERENCES artifacts(id),
  rollback_artifact_id INTEGER REFERENCES artifacts(id),
  config_patch         JSONB,
  features_patch       JSONB,
  urgency              VARCHAR(20) NOT NULL DEFAULT 'normal' CHECK (urgency IN ('low','normal','critical')),
  deadline_at          TIMESTAMPTZ,
  scheduled_at         TIMESTAMPTZ,
  completed_at         TIMESTAMPTZ,
  created_by           VARCHAR(255),
  created_at           TIMESTAMPTZ DEFAULT NOW(),
  updated_at           TIMESTAMPTZ DEFAULT NOW()
);

-- campaign_device_status
CREATE TABLE IF NOT EXISTS campaign_device_status (
  campaign_id     INTEGER REFERENCES campaigns(id) ON DELETE CASCADE,
  client_uuid     VARCHAR(255) NOT NULL,
  email           VARCHAR(255),
  status          VARCHAR(20) NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','notified','updated','failed','rolled_back')),
  version_before  VARCHAR(50),
  version_after   VARCHAR(50),
  error_message   TEXT,
  last_contact_at TIMESTAMPTZ,
  updated_at      TIMESTAMPTZ DEFAULT NOW(),
  PRIMARY KEY (campaign_id, client_uuid)
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_campaign_device_status_campaign_status
  ON campaign_device_status (campaign_id, status);

CREATE INDEX IF NOT EXISTS idx_cohort_members_identifier
  ON cohort_members (identifier_type, identifier_value);
