# RAG_IMPLEMENTATION_REPORT

## Architecture

Search and RAG remain separate. The existing `HybridSearchService` and
`posts_dense_multilingual_v1` find candidate posts. The new evidence path uses
those post IDs to query the independent `post_chunks_multilingual_v1`
collection and returns evidence only. The MCP exposes only the domain
capability `community.answer_from_knowledge`; ES, Qdrant, and raw chunk search
are not exposed as tools. The core ActionLoop file was not modified; only the
application composition resolver and capability catalogs were extended.

## Chunk Model

Migration `V7__post_chunks.sql` adds `post_chunks` with `chunk_id`, `post_id`,
`chunk_index`, `content`, `token_count`, UTF-16 `start_offset`/
`end_offset`, embedding model/version, dimension, event version, and created/
updated timestamps. Stable UUID name-based IDs make replay and citation
references deterministic. Every row is traceable to its canonical post and can
be rebuilt independently.

## Chunking Strategy

Canonical MySQL post content is split paragraph-first, with a 1,200-character
maximum and 160-character overlap. Source content is capped at 512 KiB before
chunking; surrogate pairs are not split and offsets are preserved. The splitter
supports Chinese, English, and mixed text without introducing an NLP service.
The live backfill produced 5,008 chunk rows for 209 public/searchable posts.

## Embedding Pipeline

Chunks reuse the existing multilingual HTTP embedding infrastructure:
`sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`, 384 dimensions,
L2 normalization, and the same query/document contract as post search. The
document text is a bounded labeled representation of title, tags, description,
and chunk content. No model service was added.

## Qdrant Chunk Collection

`post_chunks_multilingual_v1` is independent from `posts_dense`. It uses 384
dimensions and Cosine distance. Payload includes chunk/post IDs, chunk index and
offsets, visibility, status, event version, embedding model/version, and
dimension. Live health check: collection green, 5,008 points, 384 dimensions;
`posts_dense_multilingual_v1` remained green with 209 points.

## Projection Flow

The dedicated Kafka group `search-qdrant-chunk-projection` consumes the existing
post lifecycle projection topic. Publish/content update reads canonical MySQL
content, removes the old post chunk projection, chunks and embeds the new
content, then writes MySQL chunk rows and Qdrant points. Delete/private/hidden
states delete chunk projections. Canonical post event-version checks, stable
IDs, and Qdrant point version guards make duplicate and stale delivery safe.
The rebuild service is opt-in and was run once for the live 209-post backfill;
normal startup leaves it disabled.

## Evidence Retrieval

`EvidenceRetrievalService` calls the existing Hybrid Search contract for top
candidate posts, embeds the question with the shared encoder, and applies a
Qdrant filter constrained to those candidate post IDs. MySQL rows and canonical
post visibility/version are rechecked before returning `EvidenceChunk` values.
The service returns no answer and does not contain generation logic.

## Evidence Selection

Selection is deterministic: semantic chunk score is primary, followed by
candidate post rank, freshness, and stable chunk ID. No cross encoder, ColBERT,
learned ranker, or business counter enrichment was added.

## Grounded Generation

`community.answer_from_knowledge` passes only the question and returned
evidence to the existing host-injected LLM. The prompt forbids outside
knowledge and requires JSON output. Missing evidence returns exactly
`当前社区资料不足`; malformed or uncited non-empty output fails closed to that
response. Generation uses no new model service.

## Citation

Sources are accepted only when their chunk ID belongs to the returned evidence.
Post ID and title are rewritten from canonical evidence, so the model cannot
invent citation metadata. `POST_CHUNK` resource references carry chunk ID,
post title, and event version.

## Evaluation Dataset

`rag_evidence_dataset_v1.jsonl` contains 50 questions: 16 fact, 16
architecture, 14 multi-hop, and 4 no-answer. The strict validator reports:

- `VALID_QUERY_COUNT=50`
- `VALID_QREL_COUNT=138`
- `REMOVED_INVALID_COUNT=0`
- `MISSING_FIXTURE_COUNT=0`
- `MISSING_CHUNK_FIXTURE_COUNT=0`
- `NON_SEARCHABLE_QREL_COUNT=3`

The three draft/non-searchable qrels are retained with explicit fixture status
and excluded from public evidence expectations; they are not silently counted
as retrieval misses.

## Metrics

The 50-query live benchmark called only the protected domain evidence endpoint;
all 50 runs completed without transport errors. Initial chunk gold maps
post-level qrels to chunk 0, so these are a first evidence contract check, not
a human-validated evidence annotation set.

- Evidence Recall@5: `0.101449`
- Evidence Recall@10: `0.159420`
- Valid expected evidence chunks: `135`
- `RETRIEVAL_MISS` at 10: `114`
- No-answer false positives at 10: `40`

The result does not prove evidence retrieval quality. Gold chunk annotation must
be improved before using this metric as a production gate.

## Failure Recovery

Focused tests cover duplicate replay with stable IDs, stale event rejection,
private/visibility deletion, Qdrant unavailability, embedding unavailability,
and independent rebuild/backfill. The projection failure path propagates a
retryable provider error and never writes `know_posts`; a subsequent replay
reconstructs the chunk projection. Existing post projection code and
`posts_dense` were not changed.

## Performance

Live evidence retrieval latency:

- embedding p50/p95: `7/8 ms`
- chunk retrieval p50/p95: `21/24.55 ms`
- endpoint end-to-end p50/p95: `271.666/320.644 ms`
- generation latency: not measured live because a host LLM generation run was
  not enabled; the MCP result state records `generation_latency_ms` when used.

## Tests

- Java focused regression: PASS (`HybridSearchServiceTest`, projection tests,
  MySQL provider test, chunker, chunk projection/recovery, evidence retrieval).
- Python focused capability/generation/boundary tests: PASS, 22 tests.
- Java compile: PASS.
- RAG validator and evaluator: PASS on the frozen clean dataset.
- Live 50-query evidence benchmark: PASS as an execution run; quality gate not
  proven.
- New benchmark scripts pass Ruff. Full repository Ruff still reports existing
  unrelated lint findings in the large runtime modules.

## Git Diff

Phase 4 changes are isolated to the new RAG chunk/domain/projection/evidence
path, benchmark artifacts, Java client, MCP capability catalog/handler, and
application-level capability wiring. No merge, reset, clean, baseline-tag
change, or history rewrite was performed. The independent Phase 4 commit is
created after this report is verified and pushed to
`origin/feature/hybrid-search-rag`.

## Verdict

Pipeline implementation and projection consistency: PASS.

`RAG_RETRIEVAL_NOT_PROVEN`

`GENERATION_NOT_PROVEN`

Overall: `BLOCKED` for production RAG rollout until chunk-level gold evidence
is human/semantic validated and a real host-LLM faithfulness/citation run is
completed. Search remains unchanged and BM25/Hybrid production behavior is not
altered.
