# Memory Isolation Evaluation

Dataset: **100 scoped cases** covering two users, two
tenants, old/new Conversations, and missing tenant scope.

## Metrics

| Metric | Value |
|---|---:|
| Cross-user leakage count | 0 |
| Cross-user leakage rate | 0.0000 |
| Cross-tenant leakage count | 0 |
| Cross-tenant leakage rate | 0.0000 |
| Expected-scope miss rate | 0.0000 |
| Missing-tenant fail-closed rate | 1.0000 |
| Intentional cross-Conversation reuse rate | 1.0000 |

## Failure Cases

- None

Same-scope irrelevant returns (relevance noise, not isolation leakage):
**19**.

## Architecture Finding

User and tenant are the privacy boundaries. Conversation identity is not an
isolation boundary for Preference Memory: a preference explicitly learned in
an older Conversation is expected to be reusable in a new Conversation for
the same user and tenant. This is the intended cross-Conversation behavior.
