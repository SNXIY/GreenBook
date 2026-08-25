-- Preference Memory vertical slice: explicit scope, provenance, and lifecycle.
-- Existing ``conversation_id`` remains a compatibility column; the new
-- ``source_conversation_id`` column names the same provenance explicitly.
ALTER TABLE agent_memories
    ADD COLUMN IF NOT EXISTS tenant_id VARCHAR(128) NOT NULL DEFAULT '';

ALTER TABLE agent_memories
    ADD COLUMN IF NOT EXISTS source_conversation_id VARCHAR(128);

ALTER TABLE agent_memories
    ADD COLUMN IF NOT EXISTS status VARCHAR(32) NOT NULL DEFAULT 'active';

UPDATE agent_memories
SET source_conversation_id = conversation_id
WHERE source_conversation_id IS NULL;

CREATE INDEX IF NOT EXISTS idx_agent_memories_scope_type
    ON agent_memories (tenant_id, user_id, memory_type, status, updated_at DESC);
