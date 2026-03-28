-- Migration 006: Communications (announcements, alerts, surveys, changelogs)

CREATE TABLE IF NOT EXISTS communications (
    id SERIAL PRIMARY KEY,
    type VARCHAR(20) NOT NULL
        CHECK (type IN ('announcement', 'alert', 'survey', 'changelog')),
    title VARCHAR(300) NOT NULL,
    body TEXT NOT NULL,
    priority VARCHAR(10) DEFAULT 'normal'
        CHECK (priority IN ('low', 'normal', 'high', 'critical')),
    target_plugin_id INT REFERENCES plugins(id),
    target_cohort_id INT REFERENCES cohorts(id),
    target_bundle_id INT REFERENCES bundles(id),
    min_plugin_version VARCHAR(50),
    max_plugin_version VARCHAR(50),
    starts_at TIMESTAMPTZ DEFAULT NOW(),
    expires_at TIMESTAMPTZ,
    survey_question TEXT,
    survey_choices JSONB,
    survey_allow_multiple BOOLEAN DEFAULT false,
    survey_allow_comment BOOLEAN DEFAULT false,
    status VARCHAR(20) DEFAULT 'draft'
        CHECK (status IN ('draft', 'active', 'paused', 'completed', 'expired')),
    created_by VARCHAR(255),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_communications_status ON communications(status, starts_at);
CREATE INDEX IF NOT EXISTS idx_communications_plugin ON communications(target_plugin_id);

-- Survey responses
CREATE TABLE IF NOT EXISTS survey_responses (
    id SERIAL PRIMARY KEY,
    communication_id INT NOT NULL REFERENCES communications(id) ON DELETE CASCADE,
    client_uuid VARCHAR(255) NOT NULL,
    email VARCHAR(255),
    choices JSONB NOT NULL,
    comment TEXT,
    responded_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (communication_id, client_uuid)
);

CREATE INDEX IF NOT EXISTS idx_survey_responses_comm ON survey_responses(communication_id);

-- Message acknowledgements
CREATE TABLE IF NOT EXISTS communication_acks (
    communication_id INT NOT NULL REFERENCES communications(id) ON DELETE CASCADE,
    client_uuid VARCHAR(255) NOT NULL,
    acked_at TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (communication_id, client_uuid)
);

-- Grants
DO $$
BEGIN
  PERFORM 1 FROM pg_roles WHERE rolname = 'dev';
  IF FOUND THEN
    GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO dev;
    GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public TO dev;
  END IF;
EXCEPTION WHEN insufficient_privilege THEN NULL;
END $$;
