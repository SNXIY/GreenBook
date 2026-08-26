# Memory Retrieval Evaluation

Dataset: **100 query cases** over a three-record
Preference Memory fixture.

## Metrics

| K | Eligible target cases | Recall@K | Precision@K |
|---:|---:|---:|---:|
| 1 | 90 | 1.0000 | 1.0000 |
| 3 | 90 | 1.0000 | 0.3333 |
| 5 | 90 | 1.0000 | 0.2000 |

Irrelevant-query cases: 10; memory-return rate for
those cases: **100.00%**.

## Failure Cases

- `retrieve-091` (irrelevant_memory_returned): `Schedule a post tomorrow about Agent architecture` — expected `[]`, actual `['writing_depth', 'technology_stack', 'response_style']`
- `retrieve-092` (irrelevant_memory_returned): `Schedule a post tomorrow about distributed systems` — expected `[]`, actual `['writing_depth', 'technology_stack', 'response_style']`
- `retrieve-093` (irrelevant_memory_returned): `Schedule a post tomorrow about PostgreSQL` — expected `[]`, actual `['writing_depth', 'technology_stack', 'response_style']`
- `retrieve-094` (irrelevant_memory_returned): `Schedule a post tomorrow about MCP` — expected `[]`, actual `['writing_depth', 'technology_stack', 'response_style']`
- `retrieve-095` (irrelevant_memory_returned): `Schedule a post tomorrow about reliability` — expected `[]`, actual `['writing_depth', 'technology_stack', 'response_style']`
- `retrieve-096` (irrelevant_memory_returned): `Schedule a post tomorrow about Python services` — expected `[]`, actual `['writing_depth', 'technology_stack', 'response_style']`
- `retrieve-097` (irrelevant_memory_returned): `Schedule a post tomorrow about Java runtime` — expected `[]`, actual `['technology_stack', 'writing_depth', 'response_style']`
- `retrieve-098` (irrelevant_memory_returned): `Schedule a post tomorrow about RAG evaluation` — expected `[]`, actual `['writing_depth', 'technology_stack', 'response_style']`
- `retrieve-099` (irrelevant_memory_returned): `Schedule a post tomorrow about workflow design` — expected `[]`, actual `['writing_depth', 'technology_stack', 'response_style']`
- `retrieve-100` (irrelevant_memory_returned): `Schedule a post tomorrow about API contracts` — expected `[]`, actual `['writing_depth', 'technology_stack', 'response_style']`

## Interpretation

Targeted lexical queries generally rank the matching preference first. The
irrelevant-query return rate is an important limitation: the current bounded
retriever still returns same-scope active preferences when lexical overlap is
absent because confidence/importance/recency contribute a positive score.
This is reported as an evaluation finding, not changed in production during
this evaluation-only run.
