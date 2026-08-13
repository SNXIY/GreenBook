-- Phase17-C: body-free Artifact fields required by durable result projection.
ALTER TABLE IF EXISTS artifact_record
    ADD COLUMN IF NOT EXISTS resource_type VARCHAR(64),
    ADD COLUMN IF NOT EXISTS resource_id VARCHAR(128),
    ADD COLUMN IF NOT EXISTS title VARCHAR(500),
    ADD COLUMN IF NOT EXISTS summary VARCHAR(1000) NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS result_status VARCHAR(64),
    ADD COLUMN IF NOT EXISTS run_at VARCHAR(64),
    ADD COLUMN IF NOT EXISTS timezone VARCHAR(64),
    ADD COLUMN IF NOT EXISTS step_id VARCHAR(128) NOT NULL DEFAULT '';
