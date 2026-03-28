-- Migration 005: Plugin catalog (plugins, versions, bundles, installations)

-- Plugins (catalog entities)
CREATE TABLE IF NOT EXISTS plugins (
    id SERIAL PRIMARY KEY,
    slug VARCHAR(100) UNIQUE NOT NULL,
    name VARCHAR(200) NOT NULL,
    description TEXT,
    intent TEXT,
    key_features JSONB DEFAULT '[]'::jsonb,
    changelog TEXT,
    icon_url TEXT,
    device_type VARCHAR(50) NOT NULL,
    category VARCHAR(100) DEFAULT 'productivity',
    homepage_url TEXT,
    support_email TEXT,
    publisher VARCHAR(200) DEFAULT 'DNUM',
    visibility VARCHAR(20) DEFAULT 'public'
        CHECK (visibility IN ('public', 'internal', 'hidden')),
    status VARCHAR(20) DEFAULT 'active'
        CHECK (status IN ('draft', 'active', 'deprecated', 'removed')),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_plugins_status ON plugins(status);
CREATE INDEX IF NOT EXISTS idx_plugins_device_type ON plugins(device_type);
CREATE INDEX IF NOT EXISTS idx_plugins_category ON plugins(category);

-- Plugin versions (link plugin <-> artifact)
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
        CHECK (status IN ('draft', 'published', 'deprecated', 'yanked')),
    distribution_mode VARCHAR(20) DEFAULT 'managed'
        CHECK (distribution_mode IN ('managed', 'download_link', 'store', 'manual')),
    published_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (plugin_id, version)
);

CREATE INDEX IF NOT EXISTS idx_plugin_versions_plugin ON plugin_versions(plugin_id);
CREATE INDEX IF NOT EXISTS idx_plugin_versions_status ON plugin_versions(status);

-- Bundles (plugin groupings)
CREATE TABLE IF NOT EXISTS bundles (
    id SERIAL PRIMARY KEY,
    slug VARCHAR(100) UNIQUE NOT NULL,
    name VARCHAR(200) NOT NULL,
    description TEXT,
    icon_url TEXT,
    visibility VARCHAR(20) DEFAULT 'public'
        CHECK (visibility IN ('public', 'internal', 'hidden')),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Bundle composition
CREATE TABLE IF NOT EXISTS bundle_plugins (
    bundle_id INT NOT NULL REFERENCES bundles(id) ON DELETE CASCADE,
    plugin_id INT NOT NULL REFERENCES plugins(id) ON DELETE CASCADE,
    is_required BOOLEAN DEFAULT true,
    display_order INT DEFAULT 0,
    PRIMARY KEY (bundle_id, plugin_id)
);

-- Installation tracking
CREATE TABLE IF NOT EXISTS plugin_installations (
    id SERIAL PRIMARY KEY,
    plugin_id INT NOT NULL REFERENCES plugins(id),
    client_uuid VARCHAR(255) NOT NULL,
    email VARCHAR(255),
    installed_version VARCHAR(50),
    installed_at TIMESTAMPTZ DEFAULT NOW(),
    last_seen_at TIMESTAMPTZ DEFAULT NOW(),
    status VARCHAR(20) DEFAULT 'active'
        CHECK (status IN ('active', 'inactive', 'uninstalled')),
    UNIQUE (plugin_id, client_uuid)
);

CREATE INDEX IF NOT EXISTS idx_plugin_installations_plugin ON plugin_installations(plugin_id);
CREATE INDEX IF NOT EXISTS idx_plugin_installations_client ON plugin_installations(client_uuid);

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
