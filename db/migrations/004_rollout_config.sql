-- Migration 004: Add rollout_config to campaigns for staged rollouts
-- Supports percentage-based progressive deployment with auto-advance

ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS rollout_config JSONB DEFAULT NULL;

-- Example rollout_config:
-- {
--   "strategy": "percentage",
--   "stages": [
--     {"percent": 5,   "duration_hours": 24, "label": "canary"},
--     {"percent": 25,  "duration_hours": 48, "label": "early_adopters"},
--     {"percent": 100, "duration_hours": 0,  "label": "general_availability"}
--   ],
--   "auto_advance": true,
--   "rollback_on_failure_rate": 0.1
-- }

COMMENT ON COLUMN campaigns.rollout_config IS 'JSON config for staged rollout (percentage-based progressive deployment)';
