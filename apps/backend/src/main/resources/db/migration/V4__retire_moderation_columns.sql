-- Phase 6B: the retired Moderation product no longer owns know_posts fields.
-- Existing development databases may still carry these columns from V1.
-- MySQL 8 does not support DROP COLUMN IF EXISTS.  Resolve each legacy
-- column through information_schema so this migration is valid for both
-- legacy databases that still have the columns and clean V1 databases.
SET @drop_moderation_reason = (
    SELECT IF(
        COUNT(*) > 0,
        'ALTER TABLE know_posts DROP COLUMN moderation_reason',
        'SELECT 1'
    )
    FROM information_schema.columns
    WHERE table_schema = DATABASE()
      AND table_name = 'know_posts'
      AND column_name = 'moderation_reason'
);
PREPARE drop_moderation_reason_stmt FROM @drop_moderation_reason;
EXECUTE drop_moderation_reason_stmt;
DEALLOCATE PREPARE drop_moderation_reason_stmt;

SET @drop_moderation_task = (
    SELECT IF(
        COUNT(*) > 0,
        'ALTER TABLE know_posts DROP COLUMN moderation_task_id',
        'SELECT 1'
    )
    FROM information_schema.columns
    WHERE table_schema = DATABASE()
      AND table_name = 'know_posts'
      AND column_name = 'moderation_task_id'
);
PREPARE drop_moderation_task_stmt FROM @drop_moderation_task;
EXECUTE drop_moderation_task_stmt;
DEALLOCATE PREPARE drop_moderation_task_stmt;

UPDATE know_posts SET status = 'draft' WHERE status = 'reviewing';
