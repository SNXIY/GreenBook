---
{"name":"content-creation","version":"1.1.0","description":"Delegate durable draft creation and rewriting to Creator Agent.","domains":["content_publish","content_edit","community_operation"],"capabilities":["generation","rewrite_content"],"tools":["creator.create_draft","creator.revise_draft"],"risk":"REVERSIBLE","requires_approval":false}
---
Creator Agent owns long-form generation, versions and the handed-off Java draft.
When the user asks to imitate or continue a post, read that post first and bind
the resulting evidence as references. Creating a draft does not imply publishing
it. Do not insert an external content-review agent into ordinary AI draft generation.
Cross-turn revisions must consume the currently selected CONTENT_DRAFT artifact
and produce a new immutable version; never overwrite or guess a historical ID.
