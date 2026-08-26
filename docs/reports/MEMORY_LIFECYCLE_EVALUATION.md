# Memory Lifecycle Evaluation

Dataset: **5 lifecycle cases**; passed
**5**, failed **0**.

## Cases

| Case | Result | Details |
|---|---|---|
| same_value_update | PASS | `{'first_id': 'lifecycle-update-first', 'merged_id': 'lifecycle-update-first', 'confidence': 0.95}` |
| conflict_supersedes_without_delete | PASS | `{'old_status': 'superseded', 'new_status': 'active'}` |
| inactive_excluded_from_retrieval | PASS | `{'status': 'inactive', 'visible_ids': ['lifecycle-java']}` |
| wrong_scope_cannot_mutate | PASS | `{'result': None, 'status': 'active'}` |
| superseded_retry_does_not_resurrect | PASS | `{'retry_status': 'superseded', 'active_status': 'active'}` |

## Findings

- Same key/value evidence converges to one active record and raises confidence.
- A changed value supersedes the old record instead of deleting it.
- Inactive and superseded records are excluded by active Preference retrieval.
- Wrong user/tenant scope cannot mutate a lifecycle row.
- A retried superseded source event is idempotently ignored; the historical row stays superseded.
