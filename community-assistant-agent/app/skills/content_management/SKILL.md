---
{"name":"content-management","version":"1.0.0","description":"List, edit or delete content owned by the current user.","domains":["content_edit","content_delete"],"capabilities":["list_own_content","delete_content"],"tools":["community.list_own_posts","community.delete_post","community.delete_own_posts_batch"],"risk":"EXTERNAL_WRITE","requires_approval":true}
---
Ownership must be established by the Java user-scoped inventory, never public
search. “Delete all my posts” first enumerates the complete current-user
inventory, then binds that exact list into one approval. Do not act on another
user's resource or on model-invented identifiers.
