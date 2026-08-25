# RAG Current State

本报告用于保存当前 RAG evidence pipeline 的现场，并为后续 Memory 架构工作提供独立边界。当前分支为 `feature/hybrid-search-rag`，checkpoint 基于 `f101874`。

## Implemented

- 保留现有 Hybrid Search 的 post-level boundary：`posts_dense_multilingual_v1` 只负责返回相关帖子候选。
- 新增 post-chunk domain、`V7__post_chunks.sql` migration、可追溯的 chunk 元数据和 rebuild 支持。
- 实现 paragraph-first chunking，支持中文、英文和中英混合文本，带最大长度、overlap 与 UTF-16 offset。
- 复用 multilingual embedding infrastructure：384 dimensions、L2 normalization，以及一致的 query/document encoder contract。
- 新增独立的 `post_chunks_multilingual_v1` Qdrant collection 及 payload/version guard。
- 接入 Post lifecycle、Transactional Outbox、Kafka projection、duplicate/stale event 防护、删除/不可见 fail-closed 与 chunk rebuild/backfill。
- 实现 Evidence Retrieval、deterministic evidence selection、citation 校验和 grounded generation capability；MCP 仍只暴露业务 capability，不暴露 ES/Qdrant/chunk search。
- 建立 RAG evidence dataset v2、validator、chunk retrieval diagnosis 和 representation offline evaluation。

## Validated

- Infrastructure、projection、recovery 和现有 focused regression 均通过；RAG pipeline 状态为 `RAG_PIPELINE_PASS`。
- Dataset v2：50/50 queries 有效，66 qrels，104 条 gold chunk references，75 个 unique chunks，45 条 answerable、5 条 no-answer，annotation coverage 100%，没有 invalid 或 missing fixture。
- 诊断已定位 FIRST BAD STATE 为 `POST_RETRIEVAL -> CHUNK_RETRIEVAL`；evidence selection 没有额外 loss，candidate depth 不是主要修复点。
- 当前固定 Top10 post candidates 的 live diagnosis：POST_RECALL@10 = 0.744444，CONDITIONAL_CHUNK_RECALL@10 = 0.307692，FINAL_EVIDENCE_RECALL@10 = 0.251852。
- representation offline audit 表明当前代码实际使用的 `title + tags + description + content` 在完整 dataset 上优于 A/B/C 以及不含 tags 的 D；因此当前没有生产表示变更或 Qdrant rebuild。

## Known Limitations

- Retrieval quality 尚未达到 production gate：post recall 与 chunk recall 仍有明显损失，问题集中在 post candidate miss、chunk split 和 chunk embedding miss。
- 当前结论来自 50-query evidence-level benchmark；仍需继续做 retrieval quality optimization，但不能把历史 post-qrel 自动映射误当作 chunk ground truth。
- Grounded generation 的 faithfulness、citation correctness 和完整 live generation evaluation 尚未完成。
- 旧的诊断报告保留作为历史证据；后续工作应以 dataset v2 validator、最新 diagnosis 和 representation final decision 为准。

## Not Production Ready

当前明确状态：

- `RAG_PIPELINE_PASS`
- `RETRIEVAL_QUALITY_OPTIMIZATION_PENDING`
- `GENERATION_EVALUATION_PENDING`

RAG 暂不作为默认生产问答路径发布。该 checkpoint 不改变 Hybrid Search、projection、MCP、Agent Runtime、ActionLoop 或 Durable Runtime。

## Next Optimization Direction

1. 在冻结的 `rag-evidence-v2` 上继续拆分并提升 post retrieval 与 chunk retrieval，保持当前 production representation 作为 baseline。
2. 只在有明确实验收益后评估最小 retrieval 变更；当前不引入 reranker、query rewrite、模型替换或 chunk size 改动。
3. retrieval 达标后，单独完成 grounded generation 的 faithfulness 与 citation correctness 评估。
4. 后续 Memory 工作保持独立架构边界：不存储当前 task、execution 或 resource truth，也不修改本 RAG pipeline。

临时残留清理：已删除可安全删除的 Python cache、Java/前端 build residue 与 `.ruff_cache`；根目录 `.pytest_cache` 因文件系统权限拒绝删除，未扩大清理范围。
