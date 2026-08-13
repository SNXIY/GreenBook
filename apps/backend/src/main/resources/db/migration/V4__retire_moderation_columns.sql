-- Phase 6B: the retired Moderation product no longer owns know_posts fields.
-- Existing development databases may still carry these columns from V1.
ALTER TABLE know_posts DROP COLUMN IF EXISTS moderation_reason;
ALTER TABLE know_posts DROP COLUMN IF EXISTS moderation_task_id;
UPDATE know_posts SET status = 'draft' WHERE status = 'reviewing';
