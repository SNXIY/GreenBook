-- Phase17-C: durable structured Assistant result parts.
ALTER TABLE assistant_messages
    ADD COLUMN IF NOT EXISTS parts JSONB NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN IF NOT EXISTS run_id VARCHAR(128),
    ADD COLUMN IF NOT EXISTS execution_id VARCHAR(128);
