-- Phase18-B: durable approval ownership and execution checkpoint binding.
ALTER TABLE assistant_approvals
    ADD COLUMN IF NOT EXISTS execution_id VARCHAR(128),
    ADD COLUMN IF NOT EXISTS user_id VARCHAR(64),
    ADD COLUMN IF NOT EXISTS tenant_id VARCHAR(64),
    ADD COLUMN IF NOT EXISTS payload JSONB;
