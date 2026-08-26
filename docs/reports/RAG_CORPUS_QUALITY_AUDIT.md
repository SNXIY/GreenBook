# RAG_CORPUS_QUALITY_AUDIT

Checkpoint: `df09dc8bc30a8da4ec1bd5d71d4a13fc035056b6`

## Canonical full-content source

The audited production chain is:

`KnowPost metadata → content_object_key → OssStorageService.readTextObject → PostSearchDocumentService.build → PostChunkProjectionService → PostChunker → MySQL post_chunks + Qdrant post_chunks_multilingual_v1`

`PostSearchDocumentService.build` reads the object-store body with a 1 MiB
limit. `PostChunkProjectionService` passes `document.content` to the
paragraph-first chunker and uses title/tags/description only as labelled
embedding context. MySQL `description` is not treated as the full post body.

Current local runtime evidence: `STORAGE_PROVIDER=local`; the same
`OssStorageService` abstraction resolves objects under the configured local
storage root. No external OSS object was fetched or modified by this audit.

## Live corpus statistics

| Measure | Value |
|---|---:|
| Published + public posts | 211 |
| Object body available | 211 |
| Missing/unreadable body | 0 |
| Empty body | 0 |
| Whitespace-only body | 0 |
| Debug/placeholder body signal | 3 |
| Duplicate post-content groups | 6 |

Content length is trimmed UTF-8-decoded body characters:

| Statistic | Characters |
|---|---:|
| min | 4 |
| p25 | 1325.0 |
| p50 | 1715.0 |
| p75 | 2206.0 |
| p95 | 2969.0 |
| mean | 1682.588 |
| max | 4269 |

| Body length | Count | Rate |
|---|---:|---:|
| < 50 | 29 | 0.1374 |
| < 100 | 30 | 0.1422 |
| < 300 | 30 | 0.1422 |
| < 500 | 31 | 0.1469 |
| >= 500 | 180 | 0.8531 |

The debug/placeholder classification uses body markers, body structure, and
length; it is not inferred from title alone. Examples and per-post flags are
in the JSON artifact.

## Chunk corpus statistics

| Measure | Value |
|---|---:|
| MySQL chunk rows | 5040 |
| Qdrant points | 5040 |
| Unique MySQL chunk posts | 211 |
| Unique Qdrant chunk posts | 211 |
| Posts with 0 chunks | 0 |
| Posts with 1 chunk | 29 |
| Empty chunks | 0 |
| Tiny chunks (<50 chars) | 2629 |
| Exact duplicate chunk groups | 373 |
| Near-duplicate chunk pairs (heuristic) | 352 |

Chunks per post: p50=26.0,
p95=40.0,
max=55.

Eligible public posts and chunk projection sets are equal:
`TRUE`.
MySQL and Qdrant chunk identity sets are equal:
`TRUE`.

Posts with available body but zero chunks: **0**.
The complete list is in the JSON artifact.

## Gold corpus audit

The audit covers 29 unique gold posts, 75 unique gold chunks, and 104 gold
references. Gold post quality distribution:

| Category | Posts | Rate |
|---|---:|---:|
| `GOOD_CORPUS` | 27 | 0.9310 |
| `THIN_CONTENT` | 0 | 0.0000 |
| `MISSING_CONTENT` | 0 | 0.0000 |
| `PROJECTION_MISSING` | 0 | 0.0000 |
| `WEAK_GOLD_EVIDENCE` | 2 | 0.0690 |
| `ANNOTATION_ISSUE` | 0 | 0.0000 |

Gold chunk validity:

| Check | Count |
|---|---:|
| Gold chunks in fixture | 75 |
| Present in MySQL projection | 75 |
| Present in Qdrant collection | 75 |
| Non-empty MySQL chunk text | 75 |
| Parent body available | 75 |
| Annotation/projection issue count | 0 |

`WEAK_GOLD_EVIDENCE` is an offline semantic-screening flag for a gold post
whose maximum answer-to-gold-chunk similarity is below the `STRONG` threshold;
it does not mean the body is missing or the annotation is invalid. All
answerable dataset rows remain human-audited and all gold chunk identities are
present and non-empty.

## Knowledge coverage

Coverage is evaluated against current canonical chunk text and the dataset
gold answer with the deployed multilingual embedding model
`sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`. The semantic similarity
score is the maximum answer-to-gold-chunk cosine score; lexical overlap is
retained as an auxiliary diagnostic field. Thresholds are
`NO_REAL < 0.30`,
`WEAK < 0.40`,
`PARTIAL < 0.60`, and
`STRONG >= 0.60`.
This is an offline corpus-support signal, not a replacement for human
annotation.

| Coverage | Queries | Rate |
|---|---:|---:|
| `STRONG_COVERAGE` | 38 | 0.8444 |
| `PARTIAL_COVERAGE` | 7 | 0.1556 |
| `WEAK_COVERAGE` | 0 | 0.0000 |
| `NO_REAL_COVERAGE` | 0 | 0.0000 |

## Retrieval metrics: ALL vs STRONG_COVERAGE_ONLY

| Scope | Queries | Post Recall@10 | Conditional Chunk Recall@10 | Final Evidence Recall@10 | Chunk MRR |
|---|---:|---:|---:|---:|---:|
| ALL | 45 | 0.744444 | 0.307692 | 0.251852 | 0.141755 |
| STRONG_COVERAGE_ONLY | 38 | 0.763158 | 0.354167 | 0.280702 | 0.164108 |

## Failure-family × corpus-quality correlation

The join uses the 78 baseline missed gold references from the fixed Top10
retrieval diagnosis. `GOOD_CORPUS` is the strong-post bucket; all other gold
post categories are shown as thin/weak for this correlation.

| Failure family | Missed refs | Share of misses | Share of all gold | GOOD_CORPUS | Thin/weak/other |
|---|---:|---:|---:|---:|---:|
| `POST_CANDIDATE_FAILURE` | 24 | 0.3077 | 0.2308 | 19 | 5 |
| `LOCAL_RANKING_FAILURE` | 48 | 0.6154 | 0.4615 | 48 | 0 |
| `CHUNK_BOUNDARY_FAILURE` | 6 | 0.0769 | 0.0577 | 6 | 0 |

The detailed join also includes query coverage categories and post IDs in the
JSON artifact. This keeps `POST_CANDIDATE_FAILURE` separate from isolated
chunk ranking failures.

## Exact FIRST_BAD_STATE

The RAG retrieval diagnosis remains:

`POST_RETRIEVAL → CHUNK_RETRIEVAL`

This audit adds a corpus-quality dimension. It does not move the first bad
state to generation or evidence selection. Projection mismatches, if any,
are reported separately from retrieval ranking.

## Verdict

`CORPUS_QUALITY_LIMITING_RAG`

The live corpus is therefore not projection-complete evidence of retrieval
quality by itself: 31/211 public posts have a low-information or debug-like
signal, including 29 posts under 50 characters and 6 exact duplicate-content
groups. However, the frozen gold set is materially healthier: 27/29 gold posts
are `GOOD_CORPUS`, no gold post has missing content or projection loss, and all
48 `LOCAL_RANKING_FAILURE` plus all 6 `CHUNK_BOUNDARY_FAILURE` references map
to `GOOD_CORPUS` posts. Corpus cleanup should be evaluated before further
ranking changes, while keeping `POST_RETRIEVAL → CHUNK_RETRIEVAL` as the
retrieval first-bad-state.

No production files were changed. No Qdrant collection was rebuilt, and no
post content or dataset annotation was modified.

## Next recommendation

Quarantine or separately tag low-information/debug/duplicate public posts at
the corpus eligibility boundary, then rerun the frozen retrieval evaluation.
Do not rebuild the current collection or alter ranking in this audit phase.

## Production files changed

`[]` — evaluation script and report artifacts only.

## Generated artifacts

- [rag_corpus_quality_audit.json](../evaluation/rag_corpus_quality_audit.json)
- [audit_rag_corpus_quality.py](../../apps/backend/scripts/audit_rag_corpus_quality.py)
