-- Phase 5: durable long-term memory.  Context remains a bounded projection;
-- Task, Artifact, and Execution facts continue to live in their own tables.
CREATE TABLE IF NOT EXISTS agent_memories (
    memory_id VARCHAR(128) PRIMARY KEY,
    user_id VARCHAR(128) NOT NULL,
    conversation_id VARCHAR(128),
    task_id VARCHAR(128),
    memory_type VARCHAR(32) NOT NULL,
    content TEXT NOT NULL,
    structured_metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    importance DOUBLE PRECISION NOT NULL DEFAULT 0.5,
    confidence DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    source_type VARCHAR(64) NOT NULL DEFAULT '',
    source_id VARCHAR(128),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_accessed_at TIMESTAMPTZ,
    access_count INTEGER NOT NULL DEFAULT 0,
    expires_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_agent_memories_user_type
    ON agent_memories (user_id, memory_type, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_agent_memories_task
    ON agent_memories (user_id, task_id, updated_at DESC);
