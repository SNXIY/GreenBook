---
{"name":"content-creation","version":"1.0.0","description":"Delegate durable draft creation and rewriting to Creator Agent.","domains":["content_publish","content_edit","community_operation"],"capabilities":["generation","rewrite_content"],"tools":["creator.create_draft"],"risk":"REVERSIBLE","requires_approval":false}
---
Creator Agent owns long-form generation, versions and the handed-off Java draft.
When the user asks to imitate or continue a post, read that post first and bind
the resulting evidence as references. Creating a draft does not imply publishing
it. Do not insert Moderation Agent into ordinary AI draft generation.
