---
{"name":"publication","version":"1.0.0","description":"Publish or schedule task-owned drafts through the Java state machine.","domains":["content_publish","community_operation"],"capabilities":["publishing","schedule_publish"],"tools":["publication.publish_now","publication.schedule","publication.schedule_batch"],"risk":"EXTERNAL_WRITE","requires_approval":true}
---
Publication always uses the Java authority and a draft artifact produced by the
current run. Immediate publication and batch scheduling require exact,
version-bound approval. A future time uses scheduling. Never bypass the Java
publication state machine or publish a model-invented draft identifier.
