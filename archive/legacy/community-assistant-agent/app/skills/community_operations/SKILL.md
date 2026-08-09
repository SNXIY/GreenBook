---
{"name":"community-operations","version":"1.2.0","description":"Plan evidence-driven, multi-step and cross-turn community operation tasks.","domains":["community_operation"],"capabilities":["analysis","trend_analysis","user_insight","generation","rewrite_content","publishing","schedule_publish"],"tools":["community.analyze_engagement","community.list_active_users","community.list_posts_by_users","community.aggregate_post_topics","community.search_posts","creator.create_draft","creator.revise_draft","publication.publish_now","publication.schedule","publication.get_schedule","publication.update_schedule","publication.cancel_schedule","publication.schedule_batch"],"risk":"EXTERNAL_WRITE","requires_approval":true}
---
Decompose the goal into an evidence DAG. Independent trend and user-insight
analysis can run in parallel; creation depends on the evidence it consumes.
Publishing is a separate, approval-bound step. External content-review agents
are out of scope for this assistant and are never inserted as workflow nodes.
