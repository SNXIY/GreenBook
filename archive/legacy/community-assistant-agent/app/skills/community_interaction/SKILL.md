---
{"name":"community-interaction","version":"1.0.0","description":"Reply to an explicit comment context as the GreenBook assistant.","domains":["comment_interaction"],"capabilities":["comment_interaction"],"tools":["community.reply_comment"],"risk":"REVERSIBLE","requires_approval":false}
---
Only reply within the current post and comment context. The Java service records
the assistant identity and provenance. A comment reply does not grant permission
to edit, delete or publish unrelated content.
