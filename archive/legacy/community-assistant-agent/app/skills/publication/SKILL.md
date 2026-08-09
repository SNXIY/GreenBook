---
{"name":"publication","version":"1.1.0","description":"Publish, reschedule or cancel version-bound drafts through the Java state machine.","domains":["content_publish","content_edit","community_operation"],"capabilities":["publishing","schedule_publish"],"tools":["publication.publish_now","publication.schedule","publication.get_schedule","publication.update_schedule","publication.cancel_schedule","publication.schedule_batch"],"risk":"EXTERNAL_WRITE","requires_approval":true}
---
Publication always uses the Java authority and a draft artifact produced by the
current run. Immediate publication and batch scheduling require exact,
version-bound approval. A future time uses scheduling. Never bypass the Java
publication state machine or publish a model-invented draft identifier.
Changing a commitment must re-read the existing schedule and update it instead
of creating a duplicate. Publishing a scheduled draft now first cancels the old
commitment so it cannot publish a second time later.
