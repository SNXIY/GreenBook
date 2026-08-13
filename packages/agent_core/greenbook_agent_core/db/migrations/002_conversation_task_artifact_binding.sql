-- Phase A-1: persist the existing Conversation -> Task -> Artifact binding.
ALTER TABLE assistant_conversations
    ADD COLUMN IF NOT EXISTS active_task_id VARCHAR(128),
    ADD COLUMN IF NOT EXISTS active_artifact_id VARCHAR(128);
