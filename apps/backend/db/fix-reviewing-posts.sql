-- ============================================================
-- 修复历史 reviewing 帖子
-- ============================================================
-- 本合同必须人工执行，不得在 Flyway 中自动运行。
--
-- 步骤 1：查询当前 reviewing 帖子
-- 步骤 2：根据业务决定恢复目标状态
-- 步骤 3：执行对应 UPDATE
-- ============================================================

-- 1. 查询所有 reviewing 帖子
SELECT id, creator_id, title, status, content_origin,
       create_time,
       update_time
FROM know_posts
WHERE status = 'reviewing'
ORDER BY update_time DESC;

-- 2. 恢复为目标状态（二选一，根据业务决定）
--
-- 选项 A：恢复为 draft（作者可重新发布）
UPDATE know_posts
SET status = 'draft', update_time = NOW()
WHERE status = 'reviewing'
  AND id IN (/* 从步骤1的结果中填入要修复的 postId */);

-- 选项 B：直接发布（仅当内容已经人工确认安全）
-- UPDATE know_posts
-- SET status = 'published', publish_time = NOW(), update_time = NOW()
-- WHERE status = 'reviewing'
--   AND id IN (/* 从步骤1的结果中填入要修复的 postId */);

-- 验证
SELECT id, status, update_time
FROM know_posts
WHERE status IN ('reviewing', 'draft')
ORDER BY update_time DESC;
