# Memory Retrieval Optimization Report

V1 baseline checkpoint: `4ef8240` (`test: add memory evaluation baseline`).
V2 is the working-tree implementation of the storage-neutral relevance gate.
This report was generated without changing storage schema, extraction,
Task/Objectives, ActionLoop, MCP, or RAG.

## V2 Gate

- Candidate input: current user request plus scoped memory candidates.
- Preference relevance threshold: `0.5`.
- Preference confidence threshold: `0.5`.
- Output: selected memories, normalized relevance scores, or an explicit
  empty/no-memory result.
- ContextBuilder treats an empty result as authoritative and does not fall
  back to an unfiltered preference dump.

## Retrieval Comparison

`Precision@K` keeps the V1 harness definition (`hits / K`). The additional
`returned precision` column measures precision among the candidates actually
returned up to K, which makes the effect of no-memory filtering visible.

| K | V1 Recall@K | V2 Recall@K | V1 Precision@K | V2 Precision@K | V1 Returned Precision | V2 Returned Precision |
|---:|---:|---:|---:|---:|---:|---:|
| 1 | 1.0000 | 1.0000 | 1.0000 | 1.0000 | 1.0000 | 1.0000 |
| 3 | 1.0000 | 1.0000 | 0.3333 | 0.3333 | 0.3333 | 1.0000 |
| 5 | 1.0000 | 1.0000 | 0.2000 | 0.2000 | 0.3333 | 1.0000 |

| Metric | V1 baseline | V2 optimized |
|---|---:|---:|
| Irrelevant-query memory-return rate | 100.00% | 0.00% |

### V2 Retrieval Failures

- None

## Injection Comparison

| Metric | V1 baseline | V2 optimized |
|---|---:|---:|
| Positive preference alignment | 1.0000 | 1.0000 |
| Unnecessary injection rate (negative cases) | 1.0000 | 0.0000 |
| Harmful injection rate (all cases) | 0.2000 | 0.0000 |

### V1 Unnecessary Injection Examples

- `Schedule a post tomorrow about Agent architecture` -> `['writing_depth', 'technology_stack', 'response_style']`
- `Schedule a post tomorrow about MCP boundaries` -> `['writing_depth', 'technology_stack', 'response_style']`
- `Schedule a post tomorrow about PostgreSQL reliability` -> `['writing_depth', 'technology_stack', 'response_style']`
- `Schedule a post tomorrow about workflow design` -> `['writing_depth', 'technology_stack', 'response_style']`
- `Schedule a post tomorrow about RAG evaluation` -> `['writing_depth', 'technology_stack', 'response_style']`
- `Schedule a post tomorrow about Agent architecture` -> `['writing_depth', 'technology_stack', 'response_style']`

## Interpretation

V2 preserves targeted retrieval recall while rejecting same-scope memories
that do not clear the relevance and confidence gates. The fixed-K precision
metric may remain unchanged when a single relevant result occupies fewer than
K slots; returned precision and unnecessary/harmful injection rates expose the
actual payload-quality improvement.
