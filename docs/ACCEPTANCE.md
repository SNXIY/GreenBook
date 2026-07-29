# Acceptance checklist for GreenBook fusion

## Pre-reqs
- [ ] Root `.env` copied from `.env.example`
- [ ] MySQL applied `zhiguang-be/db/content_origin_migration.sql` (or fresh schema.sql)
- [ ] moderation-agent on :8088
- [ ] creator-agent Studio on :8092
- [ ] community-assistant-agent on :8094
- [ ] zhiguang-be on :8080 with local storage, moderation URL and Creator handoff secret
- [ ] zhiguang-fe on :5173 with `VITE_CREATOR_STUDIO_URL`

## Checks
1. Open http://127.0.0.1:5173/create — see **自己创作** and **AI 创作**
2. AI 创作 opens Creator Studio and the Studio status shows the current Java user
3. Creator validates the Java JWT through JWKS; handoff creates a Java `AI_ASSISTED` draft
4. Open `/create/manual?draftId=<id>` — content prefilled; publish → `published` (skip external review)
5. Manual compose on `/create/manual` → publish → UI shows AI 审核中; PASS → `published` / REJECT → `rejected`
6. Calling `POST /api/v1/knowposts/ai-drafts` without the service secret is rejected
7. Restarting Java while a post is `reviewing` does not lose or duplicate the moderation task
8. Home page **知光助手** opens a keyboard-accessible dialog with typed execution steps
9. “找几篇关于减肥的帖子” returns only public, published MySQL results and source titles
10. “明天上午八点发布一篇 Java 学习帖子” creates one Creator `AUTO` task, one
    Java `AI_ASSISTED` draft and one durable scheduled action
11. Restart Community Assistant after the draft step; the run reuses completed steps and
    does not create a second draft
12. A scheduled action cannot publish a `MANUAL` draft or another user's draft
13. Comment `@助手 总结这个帖子` uses that post as context and shows the persisted reply

## Ports
| Service | Port |
|---------|------|
| zhiguang-be | 8080 |
| zhiguang-fe | 5173 |
| creator-agent | 8092 |
| moderation-agent | 8088 |
| community-assistant-agent | 8094 |
