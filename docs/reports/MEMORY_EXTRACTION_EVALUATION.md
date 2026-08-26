# Memory Extraction Evaluation

Dataset: **114 cases**.

The labels distinguish explicit reusable preferences from one-off requests and
ambiguous boundary language. The evaluator calls the existing deterministic
`PreferenceMemoryExtractor`; it does not alter extraction policy.

## Metrics

| Metric | Value |
|---|---:|
| Precision | 0.8889 |
| Recall | 0.7619 |
| False positive rate | 0.1176 |
| False negative rate | 0.2381 |
| Accuracy | 0.8158 |

Confusion matrix: TP=48, FP=6, TN=45, FN=15.

## Category Breakdown

| Category | Cases | TP | FP | FN |
|---|---:|---:|---:|---:|
| should_save_preference | 48 | 39 | 0 | 9 |
| should_not_save_one_off_or_invalid | 36 | 0 | 0 | 0 |
| boundary_case | 30 | 9 | 6 | 6 |

## Failure Cases

- `extract-002` (FN): `I prefer not to use clickbait titles across my technical guides.` — expected `{'write': True, 'memory_type': 'preference', 'preference_key': 'title_style'}`, actual `{'write': False, 'preference_key': '', 'reason': 'no_high_confidence_long_term_preference', 'confidence': 0.0}`
- `extract-011` (FN): `Please remember that I like concise replies when drafting tutorials.` — expected `{'write': True, 'memory_type': 'preference', 'preference_key': 'response_style'}`, actual `{'write': False, 'preference_key': '', 'reason': 'no_high_confidence_long_term_preference', 'confidence': 0.0}`
- `extract-012` (FN): `My default is concise replies for my writing.` — expected `{'write': True, 'memory_type': 'preference', 'preference_key': 'response_style'}`, actual `{'write': False, 'preference_key': '', 'reason': 'no_high_confidence_long_term_preference', 'confidence': 0.0}`
- `extract-018` (FN): `I prefer not to use clickbait titles when drafting tutorials.` — expected `{'write': True, 'memory_type': 'preference', 'preference_key': 'title_style'}`, actual `{'write': False, 'preference_key': '', 'reason': 'no_high_confidence_long_term_preference', 'confidence': 0.0}`
- `extract-027` (FN): `Please remember that I like concise replies for my writing.` — expected `{'write': True, 'memory_type': 'preference', 'preference_key': 'response_style'}`, actual `{'write': False, 'preference_key': '', 'reason': 'no_high_confidence_long_term_preference', 'confidence': 0.0}`
- `extract-028` (FN): `My default is concise replies in future posts.` — expected `{'write': True, 'memory_type': 'preference', 'preference_key': 'response_style'}`, actual `{'write': False, 'preference_key': '', 'reason': 'no_high_confidence_long_term_preference', 'confidence': 0.0}`
- `extract-034` (FN): `I prefer not to use clickbait titles for my writing.` — expected `{'write': True, 'memory_type': 'preference', 'preference_key': 'title_style'}`, actual `{'write': False, 'preference_key': '', 'reason': 'no_high_confidence_long_term_preference', 'confidence': 0.0}`
- `extract-043` (FN): `Please remember that I like concise replies in future posts.` — expected `{'write': True, 'memory_type': 'preference', 'preference_key': 'response_style'}`, actual `{'write': False, 'preference_key': '', 'reason': 'no_high_confidence_long_term_preference', 'confidence': 0.0}`
- `extract-044` (FN): `My default is concise replies across my technical guides.` — expected `{'write': True, 'memory_type': 'preference', 'preference_key': 'response_style'}`, actual `{'write': False, 'preference_key': '', 'reason': 'no_high_confidence_long_term_preference', 'confidence': 0.0}`
- `extract-086` (FN): `I like Java.` — expected `{'write': True, 'memory_type': 'preference', 'preference_key': 'technology_stack'}`, actual `{'write': False, 'preference_key': '', 'reason': 'no_high_confidence_long_term_preference', 'confidence': 0.0}`
- `extract-087` (FP): `I currently prefer concise replies.` — expected `{'write': False, 'memory_type': 'preference'}`, actual `{'write': True, 'preference_key': 'response_style', 'reason': 'explicit durable response-style preference', 'confidence': 0.9}`
- `extract-088` (FP): `For this article, prefer technical depth.` — expected `{'write': False, 'memory_type': 'preference'}`, actual `{'write': True, 'preference_key': 'writing_depth', 'reason': 'explicit durable content-depth preference', 'confidence': 0.9}`
- `extract-089` (FN): `My default is concise replies.` — expected `{'write': True, 'memory_type': 'preference', 'preference_key': 'response_style'}`, actual `{'write': False, 'preference_key': '', 'reason': 'no_high_confidence_long_term_preference', 'confidence': 0.0}`
- `extract-096` (FN): `I like Java.` — expected `{'write': True, 'memory_type': 'preference', 'preference_key': 'technology_stack'}`, actual `{'write': False, 'preference_key': '', 'reason': 'no_high_confidence_long_term_preference', 'confidence': 0.0}`
- `extract-097` (FP): `I currently prefer concise replies.` — expected `{'write': False, 'memory_type': 'preference'}`, actual `{'write': True, 'preference_key': 'response_style', 'reason': 'explicit durable response-style preference', 'confidence': 0.9}`
- `extract-098` (FP): `For this article, prefer technical depth.` — expected `{'write': False, 'memory_type': 'preference'}`, actual `{'write': True, 'preference_key': 'writing_depth', 'reason': 'explicit durable content-depth preference', 'confidence': 0.9}`
- `extract-099` (FN): `My default is concise replies.` — expected `{'write': True, 'memory_type': 'preference', 'preference_key': 'response_style'}`, actual `{'write': False, 'preference_key': '', 'reason': 'no_high_confidence_long_term_preference', 'confidence': 0.0}`
- `extract-106` (FN): `I like Java.` — expected `{'write': True, 'memory_type': 'preference', 'preference_key': 'technology_stack'}`, actual `{'write': False, 'preference_key': '', 'reason': 'no_high_confidence_long_term_preference', 'confidence': 0.0}`
- `extract-107` (FP): `I currently prefer concise replies.` — expected `{'write': False, 'memory_type': 'preference'}`, actual `{'write': True, 'preference_key': 'response_style', 'reason': 'explicit durable response-style preference', 'confidence': 0.9}`
- `extract-108` (FP): `For this article, prefer technical depth.` — expected `{'write': False, 'memory_type': 'preference'}`, actual `{'write': True, 'preference_key': 'writing_depth', 'reason': 'explicit durable content-depth preference', 'confidence': 0.9}`

## Interpretation

False negatives are expected for conservative boundary language such as “I
like Java” because the MVP requires a stronger technology-stack signal. False
positives identify wording that combines a durable marker with a task-local
qualifier and should be reviewed before expanding the extractor vocabulary.
