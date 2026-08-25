# GreenBook Capability Matrix

Date: 2026-08-21

This matrix is based on the current API composition root, registered MCP
tools, Java Agent Facade controllers/services, frontend projections, and the
existing targeted tests. `WORKING` means that the capability has a real
end-to-end implementation contract in the source; it does not mean that a
live Java deployment was available in this workspace. Live verification is
called out explicitly in the last column.

| Capability | Frontend | Agent/control path | Tool | Java API | Java DB/business owner | Status | Evidence / limitation |
| --- | --- | --- | --- | --- | --- | --- | --- |
| CHAT | AgentPanel message API | `TurnCoordinator` CHAT result | none | none | Conversation service/store | WORKING | Direct chat projection exists; no Java mutation |
| SEARCH_POSTS | AgentPanel + Run/Activity projection | FastPath read or ActionLoop | `community.search_public_posts` | `GET /api/v1/agent/posts/search` | Published public post rows + engagement counters | WORKING | Real search and hot/latest sort exist; live service unavailable here |
| GET_POST | AgentPanel result card | FastPath read / ActionLoop read | `community.get_post` | `GET /api/v1/agent/posts/{postId}` | Published public post + content object | WORKING | Public/visibility checks are Java-owned |
| SUMMARIZE | Conversation result | LLM composition over bounded read evidence | no dedicated summary tool | no dedicated summary endpoint | Source post remains Java truth | PARTIAL | Read-and-summarize path exists, but summary is not a typed business Tool |
| CREATE_DRAFT | Draft/activity projection | ActionLoop `CREATE_DRAFT` | `content.create_draft` | `POST /api/v1/agent/drafts` | Java Draft + content object/metadata | WORKING | Java returns and re-reads the draft; idempotency header supported |
| REVISE_DRAFT | Draft update UI | ActionLoop `UPDATE_DRAFT` | `content.update_draft` | `PUT /api/v1/agent/drafts/{draftId}` | Java Draft with version check | WORKING | Existing draft target must resolve uniquely; Java optimistic conflict is real |
| PUBLISH_NOW | Approval + activity/result projection | FastPath/ActionLoop -> durable write | `publication.publish_now` | `POST /api/v1/agent/publications/publish-now` | Java Post publication state | WORKING | Approval and Java verification are implemented; live proof pending |
| CREATE_SCHEDULE | Activity projection | ActionLoop `CREATE_SCHEDULE` | `publication.schedule` | `POST /api/v1/agent/publications/schedules` | Java Schedule | WORKING | Canonical future time is required; tool verifies schedule after write |
| UPDATE_SCHEDULE | Activity projection | FastPath/ActionLoop `UPDATE_SCHEDULE` | `publication.update_schedule` | `PUT /api/v1/agent/publications/schedules/{scheduleId}` | Java Schedule with CAS/ownership | WORKING | Existing Schedule target and new canonical time are required |
| CANCEL_SCHEDULE | Activity projection | FastPath/ActionLoop `CANCEL_SCHEDULE` | `publication.cancel_schedule` | `DELETE /api/v1/agent/publications/schedules/{scheduleId}` | Java Schedule status | WORKING | Existing Schedule target required; durable write boundary retained |
| DELETE_POST | Risk approval card + result | FastPath/ActionLoop -> Approval -> durable write | `community.delete_post` | `DELETE /api/v1/agent/posts/{postId}` | Java owned Post deletion state | WORKING | No delete before explicit approval in the current contract |
| LIST_MY_CONTENT | AgentPanel task/content views | FastPath/ActionLoop read | `community.list_own_posts`, `content.list_drafts` | `GET /me/posts`, `GET /me/drafts` | Java owned post/draft lists | PARTIAL | Posts and drafts are separate capabilities; no unified account-content view |
| MULTI_OBJECTIVE | Run/Activity can show multiple child actions | Objective-scoped ActionLoop + ResourceBinding | multiple typed tools | multiple Java endpoints | Java resources; Run is aggregate | PARTIAL | Unit/contract coverage exists; no production Commitment confirmation/freeze or live E2E evidence |
| CROSS_TURN_UPDATE | Follow-up message + activity | TaskDelta + TargetResolver + ActionLoop | update/cancel/revise tools | existing resource endpoints | Existing Java Schedule/Draft/Post | PARTIAL | Existing-target routing exists; Commitment version/frozen stale-worker protection is only in B POC |
| TARGET_CLARIFICATION | ASK_USER/clarification projection | TargetResolver: 0/1/>1 states | none until resolved | none until resolved | Java resource identity | WORKING | Ambiguous and not-found targets stop the write path |
| TEMPORAL_CLARIFICATION | ASK_USER/clarification projection | TemporalResolver -> canonical `run_at` | schedule/update tools only after resolve | schedule endpoints | Java Schedule `run_at` | WORKING | Unresolved future cannot fall back to PUBLISH_NOW |
| HITL_DELETE | Approval card and approve/reject actions | ApprovalRuntimeService + durable resume | delete tool | Java DELETE endpoint | Java Post | WORKING | Approval is a distinct durable boundary from clarification |
| PARTIAL_SUCCESS | Run/activity status and per-action projection | Objective/Run aggregation | independent child tools | independent Java facts | Java resources, Run aggregation | PARTIAL | Status vocabulary and isolation logic exist; live fan-out evidence unavailable |
| RESULT_UNKNOWN | User Activity `RESULT_UNKNOWN` projection | OperationLedger + reconciliation worker | write tools emit unknown | Java authoritative queries | Java resource state | WORKING | Fault-injection/reconciliation tests exist; live Java fault test not run |
| NOTIFICATION_AFTER_PUBLISH | Activity/result surface | no second scheduler; waits for Java result | no Agent notification tool | Java scheduler/outbox/notification service | Java Post + outbox | PARTIAL | Java wiring exists; full publish -> notification -> frontend chain not live-tested |
| HOT_TOPIC_ANALYSIS | Search result presentation | same search path; no recommendation agent | `community.search_public_posts` | search endpoint with `sort=hot` | Java hotScore from recency/engagement | PARTIAL | Real hotScore exists, but richer diversity/quality/relationship analysis is a GAP |
| DATA_ANALYSIS | Analytics cards/results | ActionLoop read | `analytics.get_post_performance`, `analytics.get_account_summary` | post/account analytics endpoints | Java analytics queries | WORKING | Real Tool and Java endpoints exist; live proof pending |
| COMMENTS / INTERACTION | Comment/reply UI + approval | ActionLoop read/write | `interaction.list_comments`, `interaction.send_reply` | comments/reply endpoints | Java comment/reply state | WORKING | Reply is approval-gated; live proof pending |
| LONG_TERM_OPERATIONS | no verified product surface | no dedicated long-running Agent operation | no production Tool | Java publication scheduler only | Java scheduler handles publication, not operations | MISSING | Record as extension GAP; do not add a Python scheduler or mock Tool |

## Immediate conclusions

1. CORE Draft, Schedule, Post, target/time resolution, approval, unknown-result
   recovery and Java endpoints are present in the real code path.
2. The largest control-plane gaps are semantic confirmation/freeze/versioning,
   stronger cross-turn supersession, and live business-chain acceptance—not a
   missing Durable Runtime.
3. Search is a real community capability. Its current hot ranking is a Java
   `hotScore` based on recency and engagement; richer hotspot analysis remains
   an extension GAP.
4. No missing extension capability should be filled with a fake production
   Tool, a second Agent, or a second scheduler.

