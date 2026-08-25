# RAG_DATASET_V2_REPORT

Generated: 2026-08-25

## Annotation Strategy

- Retained all 50 base queries, rag-001 through rag-050.
- Each v2 row explicitly stores query, category, gold_answer, gold_post_ids, and gold_chunk_ids. gold_chunks remains only as an evaluator compatibility view.
- Gold chunks are manually selected from the MySQL canonical post_chunks snapshot. They are not derived from post qrels or from an automatic chunk_index=0 rule.
- Every answerable query has human_audited status and evidence_claims linked to explicit chunk IDs.
- rag-047 through rag-050 remain no-answer. rag-030 is also no-answer because the corpus has no directly attributable GreenBook Agent-engineering evidence.

## Dataset Statistics

| Item | Value |
|---|---:|
| Dataset version | rag_evidence_v2 |
| Total queries | 50 |
| Fact / Architecture / Multi-hop / No-answer | 16 / 15 / 14 / 5 |
| Valid queries | 50 |
| Valid qrels | 66 |
| Gold chunk references | 104 |
| Gold references at chunk_index=0 | 2 |
| Gold references at non-zero index | 102 |
| Automatic all-index-0 mapping detected | false |
| Unique gold chunks | 75 |
| Unique gold posts | 29 |
| Answerable queries | 45 |
| Annotation coverage | 45 / 45 = 100% |
| Removed invalid | 0 |
| Missing chunk fixture | 0 |

## Validation

The v2 validator status is VALID.

Passed checks:

- query is non-empty and UTF-8 clean;
- query IDs are unique;
- post and chunk IDs are parseable;
- every gold chunk ID exists in the canonical snapshot;
- chunk ID, post ID, and chunk index are consistent;
- 75/75 snapshot chunks have non-empty content and are published/public;
- gold answer, evidence claim, and gold chunk ID linkage is complete;
- no-answer rows contain no fabricated evidence;
- no qrel-to-chunk fallback exists.

Manual annotation corrections:

- rag-005 replaced a title-only chunk with a canonical chunk containing parameter validation, exception handling, and resource release.
- rag-030 changed from answerable to no-answer instead of using generic Agent articles as GreenBook-specific evidence.

## Retrieval Metrics

Evaluation used the existing Search -> Qdrant chunk retrieval -> deterministic evidence path with topPosts=10 and topChunks=10. No Hybrid Search, Chunking, Embedding, Qdrant, or Evidence Selection code was changed.

| Metric | Value |
|---|---:|
| POST_RECALL@5 | 0.555556 |
| POST_RECALL@10 | 0.744444 |
| CONDITIONAL_CHUNK_RECALL@5 | 0.239583 |
| CONDITIONAL_CHUNK_RECALL@10 | 0.307692 |
| RAW_CHUNK_RECALL@5 | 0.166667 |
| RAW_CHUNK_RECALL@10 | 0.251852 |
| FINAL_EVIDENCE_RECALL@5 | 0.166667 |
| FINAL_EVIDENCE_RECALL@10 | 0.251852 |
| Selection loss @10 | 0 |

The v2 benchmark has zero DATASET_ISSUE. Final evidence recall equals raw chunk recall; selection loss is zero.

## Failure Cases

The following are the first failures for 20 distinct queries. Full candidate/chunk details for all 50 queries are in [rag_retrieval_diagnosis_v2_20260825.json](./rag_retrieval_diagnosis_v2_20260825.json).

| Query | Dataset category | Gold post | Gold chunk | First failure | Split signal | Any gold post @10 |
|---|---|---|---|---|---|---|
| rag-001 | fact | 350308965579624448 | ee64efa0-ced8-3d09-8079-94640e528b96 | post missing | no | yes |
| rag-002 | fact | 350308965579624448 | ee64efa0-ced8-3d09-8079-94640e528b96 | post missing | no | yes |
| rag-003 | fact | 350297994190524416 | d2665fbc-a38a-3670-9245-7864b3744540 | wrong chunk | yes | yes |
| rag-004 | fact | 350118465648070656 | dfef4e49-4c1c-37f1-930d-1fc17bacf62b | embedding issue | no | yes |
| rag-005 | fact | 350118465648070656 | dfef4e49-4c1c-37f1-930d-1fc17bacf62b | post missing | no | no |
| rag-006 | fact | 350125174705754112 | a25e1e4e-0f0b-3913-be50-2a9c37f5b2a7 | wrong chunk | yes | yes |
| rag-007 | fact | 350297994190524416 | d2665fbc-a38a-3670-9245-7864b3744540 | post missing | no | yes |
| rag-009 | fact | 350308965579624448 | ee64efa0-ced8-3d09-8079-94640e528b96 | post missing | no | yes |
| rag-010 | fact | 350329167449034752 | 138dc67d-0551-32c2-8f3f-2538dfb96e80 | post missing | no | no |
| rag-011 | fact | 349537913240948736 | 6324e49c-10d8-335b-b8f3-88d9dc25de55 | post missing | no | no |
| rag-012 | fact | 349768387200684032 | 6c769e67-f3b9-3237-b3cb-13d88f8ed184 | embedding issue | no | yes |
| rag-013 | fact | 350234112948310016 | 92d82d5d-7798-336e-9d15-3b00a535eea3 | wrong chunk | yes | yes |
| rag-015 | fact | 349815075202273280 | 73919788-d3d3-315e-a039-43cd69e71b71 | wrong chunk | yes | yes |
| rag-016 | fact | 349570392173711360 | 0a23191d-9e4c-386f-a1a2-ca166fa48e5d | post missing | no | yes |
| rag-017 | architecture | 349537913240948736 | 8d7f77f9-bd99-31e5-be70-cd1bc03ac4d4 | post missing | no | no |
| rag-020 | architecture | 349853528392601600 | 6c573f34-26a6-31bc-b5a8-18ef4e96abca | wrong chunk | yes | yes |
| rag-021 | architecture | 350275996995424256 | 32fa8b13-4048-3269-8c6c-a656ccf0bfeb | wrong chunk | yes | yes |
| rag-022 | architecture | 350309267875696640 | 0201c4a2-b0a1-3225-9b3f-7e34167e03c0 | wrong chunk | yes | yes |
| rag-023 | architecture | 350275996995424256 | 78d79da5-9e7c-30f1-bb20-0e5a607c0955 | wrong chunk | yes | yes |
| rag-024 | architecture | 350275996995424256 | 182cfb4e-4bb1-3c92-bdc3-29a21f5cc422 | wrong chunk | yes | yes |

## Remaining Issues

- FIRST BAD STATE is now RETRIEVAL_ISSUE; DATASET_ISSUE was eliminated by v2 validation.
- Retrieval-issue query count: 40; dataset-issue query count: 0.
- Failure-category counts are gold-chunk failure instances and overlap: post missing 24, embedding issue 11, wrong chunk 43.
- chunk_split_signal appears in 43 failure instances; it is diagnostic evidence only and does not authorize a chunking change in this phase.
- No model, chunk, prompt, Memory, or Agent changes were made. Retrieval quality remains unproven.

## Tests

- validate_rag_dataset_v2.py --fail-on-invalid: PASS
- Existing validate_rag_dataset.py compatibility validation: PASS
- Live retrieval diagnosis for 50 queries: PASS
- git diff --check: PASS

## Git Diff

This phase only adds dataset/evaluation artifacts and v2 annotation metadata handling in the diagnosis script:

- apps/backend/scripts/validate_rag_dataset_v2.py
- apps/backend/scripts/diagnose_rag_retrieval.py
- docs/evaluation/rag_evidence_dataset_v2.jsonl
- docs/evaluation/rag_evidence_chunk_fixture_v2.json
- docs/evaluation/rag_evidence_post_fixture_v2.json
- docs/evaluation/rag_evidence_dataset_v2.validation.json
- docs/evaluation/rag_retrieval_diagnosis_v2_20260825.json

No production Search, RAG, Chunking, Embedding, Qdrant, or Agent code was modified.

## Verdict

RAG_DATASET_V2_VALID

The evidence-level benchmark is constructed and validated. The remaining blocker is retrieval quality, not dataset integrity.

RETRIEVAL_NOT_PROVEN
