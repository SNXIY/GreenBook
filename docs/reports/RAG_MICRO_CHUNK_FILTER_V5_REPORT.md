# RAG_MICRO_CHUNK_FILTER_V5_REPORT

V4 evaluation checkpoint: `3f10819`
V4 baseline checkpoint: `722a072e08f98dd6c2dd8b429c8651761244e4d9`

## Verdict

`RAG_MICRO_CHUNK_FILTER_NO_GAIN`

Chosen strategy: `None`

## Frozen snapshot

| Item | Value |
|---|---:|
| Snapshot version | `rag_retrieval_frozen_snapshot_v1` |
| Snapshot digest | `be65bfed2d90405472e0c19a6f472861ea23625b5372d4b7fa62b1df80c5de09` |
| Answerable queries | 45 |
| Candidate posts | 130 |
| Candidate chunks captured | 11148 |
| Chunk catalog | 5040 |
| Candidate post depth | 10 |
| Output chunk depth | 10 |
| Collection | `post_chunks_multilingual_v1` |
| Capture-vs-V3 Top10 drift | 6 (rag-008, rag-011, rag-012, rag-014, rag-016, rag-017) |

The V3 Top10 post pool is held fixed. All V5 ranking inputs are read from this
file; no MySQL, Qdrant, collection rebuild, embedding, or post retrieval is
performed by the evaluator.

## Snapshot reproducibility

| Check | Result |
|---|---|
| Frozen snapshot drift during rerun | `0` |
| Run fingerprint equality | `True` |
| Snapshot digest equality | `True` |
| Status | `PASS` |

Two reruns use the same immutable snapshot. The six capture-vs-V3 differences
are historical source drift and are not rerun drift.

## LT30/LT50/LT80/LT100 and hard/soft comparison

### ALL answerable queries

| Strategy | Cond R@5 | Cond R@10 | Final R@10 | MRR | Hit@1 | Hit@3 | Hit@5 | Hit@10 | Avg candidates | Avg selected | Dup waste | Gold removed | Gold harmed | Rank p95 ms |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| CURRENT_SNAPSHOT_BASELINE | 0.209402 | 0.307692 | 0.251852 | 0.156315 | 0.022222 | 0.244444 | 0.266667 | 0.422222 | 247.73 | 10.00 | 0.0489 | 0.0000 | 0.0000 | 0.508 |
| HARD_ELIGIBILITY_LT30 | 0.209402 | 0.371795 | 0.296296 | 0.178431 | 0.044444 | 0.244444 | 0.266667 | 0.466667 | 140.38 | 10.00 | 0.0311 | 0.0000 | 0.0000 | 0.345 |
| HARD_ELIGIBILITY_LT50 | 0.217949 | 0.393162 | 0.314815 | 0.183325 | 0.044444 | 0.244444 | 0.288889 | 0.511111 | 116.47 | 10.00 | 0.0311 | 0.0000 | 0.0000 | 0.263 |
| HARD_ELIGIBILITY_LT80 | 0.260684 | 0.371795 | 0.296296 | 0.184463 | 0.044444 | 0.244444 | 0.355556 | 0.488889 | 86.53 | 10.00 | 0.0400 | 0.1750 | 0.0375 | 0.221 |
| HARD_ELIGIBILITY_LT100 | 0.273504 | 0.367521 | 0.292593 | 0.190026 | 0.044444 | 0.244444 | 0.377778 | 0.488889 | 70.36 | 10.00 | 0.0444 | 0.2375 | 0.0625 | 0.173 |
| SOFT_PENALTY_LT50_P020 | 0.209402 | 0.337607 | 0.277778 | 0.159758 | 0.022222 | 0.244444 | 0.266667 | 0.466667 | 247.73 | 10.00 | 0.0444 | 0.0000 | 0.0000 | 0.579 |
| SOFT_PENALTY_LT50_P040 | 0.209402 | 0.376068 | 0.300000 | 0.179620 | 0.044444 | 0.244444 | 0.266667 | 0.488889 | 247.73 | 10.00 | 0.0333 | 0.0000 | 0.0000 | 0.511 |
| SOFT_PENALTY_LT50_P060 | 0.217949 | 0.376068 | 0.300000 | 0.181516 | 0.044444 | 0.244444 | 0.288889 | 0.488889 | 247.73 | 10.00 | 0.0333 | 0.0000 | 0.0000 | 0.534 |

### STRONG_COVERAGE_ONLY

The JSON artifact contains the same columns for the 38 strong-coverage query
subset. The report's primary table is ALL; the strong-scope values are listed
in the strategy records and summarized below.

| Strategy | Strong Cond R@10 | Strong Final R@10 | Strong MRR | Strong Gold removed | Strong Gold harmed |
|---|---:|---:|---:|---:|---:|
| CURRENT_SNAPSHOT_BASELINE | 0.354167 | 0.280702 | 0.175448 | 0.0000 | 0.0000 |
| HARD_ELIGIBILITY_LT30 | 0.401042 | 0.320175 | 0.199289 | 0.0000 | 0.0000 |
| HARD_ELIGIBILITY_LT50 | 0.427083 | 0.342105 | 0.204075 | 0.0000 | 0.0000 |
| HARD_ELIGIBILITY_LT80 | 0.401042 | 0.320175 | 0.207997 | 0.1571 | 0.0429 |
| HARD_ELIGIBILITY_LT100 | 0.395833 | 0.315789 | 0.214285 | 0.2286 | 0.0714 |
| SOFT_PENALTY_LT50_P020 | 0.390625 | 0.311404 | 0.178571 | 0.0000 | 0.0000 |
| SOFT_PENALTY_LT50_P040 | 0.406250 | 0.324561 | 0.200443 | 0.0000 | 0.0000 |
| SOFT_PENALTY_LT50_P060 | 0.406250 | 0.324561 | 0.202638 | 0.0000 | 0.0000 |

The soft experiments use fixed penalties `0.020`, `0.040`, and `0.060` only;
this is a bounded sensitivity check, not a broad parameter search.

## Gold short-chunk safety audit

| Measure | Value |
|---|---:|
| All `<50` chunks | 2629 |
| Gold references `<50` | 0 |
| Structural-noise heuristic | 1737 |
| Potential standalone useful evidence | 857 |
| Ambiguous | 35 |
| Hard LT50 ALL gold removed | 0.0000 |

The classification is a conservative corpus heuristic, not a human semantic
label. Examples are preserved in the JSON artifact. A non-zero potential
standalone-evidence count means hard deletion is not considered gold-safe by
itself; a soft penalty must be preferred if it meets the retrieval gate.

## Acceptance decision

Acceptance requires frozen rerun status PASS, ALL Conditional Recall@10 near
the V4 LT50 target (`>=0.39`), strong Conditional Recall@10 (`>=0.42`), MRR
improvement over the frozen baseline, gold removed/harmed rates at most 1%, no
output-depth increase, and no material ranking-latency regression.

| Strategy | Accepted | Reasons |
|---|---|---|
| CURRENT_SNAPSHOT_BASELINE | False | all_conditional_recall_below_0.39, strong_conditional_recall_below_0.42, mrr_not_improved |
| HARD_ELIGIBILITY_LT30 | False | all_conditional_recall_below_0.39, strong_conditional_recall_below_0.42, hard_filter_not_safe_with_potential_short_evidence |
| HARD_ELIGIBILITY_LT50 | False | hard_filter_not_safe_with_potential_short_evidence |
| HARD_ELIGIBILITY_LT80 | False | all_conditional_recall_below_0.39, strong_conditional_recall_below_0.42, gold_removed_over_1_percent, strong_gold_removed_over_1_percent, gold_harmed_over_1_percent, strong_gold_harmed_over_1_percent, hard_filter_not_safe_with_potential_short_evidence |
| HARD_ELIGIBILITY_LT100 | False | all_conditional_recall_below_0.39, strong_conditional_recall_below_0.42, gold_removed_over_1_percent, strong_gold_removed_over_1_percent, gold_harmed_over_1_percent, strong_gold_harmed_over_1_percent, hard_filter_not_safe_with_potential_short_evidence |
| SOFT_PENALTY_LT50_P020 | False | all_conditional_recall_below_0.39, strong_conditional_recall_below_0.42 |
| SOFT_PENALTY_LT50_P040 | False | all_conditional_recall_below_0.39, strong_conditional_recall_below_0.42 |
| SOFT_PENALTY_LT50_P060 | False | all_conditional_recall_below_0.39, strong_conditional_recall_below_0.42 |

## Remaining failure families

The V4 48-case local-failure labels are rechecked against the chosen frozen
selection. They are not relabeled as new ranking evidence.

| Family | V4 baseline | Resolved | Remaining |
|---|---:|---:|---:|
| `DUPLICATE_CHUNK_NOISE` | 8 | 0 | 8 |
| `GENUINE_SEMANTIC_RANKING_FAILURE` | 8 | 0 | 8 |
| `GOLD_CHUNK_TOO_FRAGMENTED` | 4 | 0 | 4 |
| `MICRO_CHUNK_NOISE` | 25 | 0 | 25 |
| `SAME_POST_FRAGMENT_COMPETITION` | 3 | 0 | 3 |

## Production and collection status

- Production files changed: `[]`
- `post_chunks_multilingual_v1`: retained unchanged
- `post_chunks_multilingual_v2`: not created
- MySQL chunks: not modified
- Qdrant: read-only
- PostChunker: not modified
- Candidate post depth: fixed at Top10

## Tests and validation

- Snapshot capture completed read-only.
- V5 evaluator ran twice against the same frozen snapshot.
- Ruff and Python syntax validation passed for the V5 scripts.
- No Memory, Java, browser, L1/L2/L3, or unrelated suites were run.

## Next recommendation

Keep production retrieval unchanged. The frozen experiment did not produce a strategy that meets the recall, MRR, and gold-safety gates simultaneously; do not promote a hard filter or create post_chunks_multilingual_v2 from this result.
