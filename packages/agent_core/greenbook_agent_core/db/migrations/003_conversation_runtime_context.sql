-- Phase17: durable timezone and long-lived conversation context fields.
ALTER TABLE assistant_conversations
    ADD COLUMN IF NOT EXISTS timezone VARCHAR(64) NOT NULL DEFAULT 'Asia/Shanghai',
    ADD COLUMN IF NOT EXISTS conversation_summary TEXT;
