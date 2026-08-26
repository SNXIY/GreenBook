# SEMANTIC_MEMORY_DESIGN

Status: `DESIGN_ONLY`

- Project: `D:\agent\green-book`
- Branch: `feature/hybrid-search-rag`
- Current checkpoint: `24d2fc6a484fc8651288584f546ec17697dc3d4e`
- No production code, schema, repository, retriever, vector collection, commit, or push was changed for this design.

## 1. Current Memory Model Audit

### 1.1 Existing durable contract

`MemoryRecord` already has the fields needed for a bounded Semantic record:

| Existing field | Semantic use |
|---|---|
| `user_id`, `tenant_id` | Mandatory scope on write and read. |
| `memory_type` | `SEMANTIC`; see the Preference alias caveat below. |
| `content` | Bounded `normalized_fact` rendered for retrieval/context. |
| `structured_metadata` | Contract marker, subject, predicate, object, fact evidence, and internal provenance. |
| `confidence`, `importance` | Admission and shared relevance-gate inputs. |
| `source_type`, `source_id` | Stable source identity and audit reference. |
| `status` | Existing `ACTIVE`, `INACTIVE`, and `SUPERSEDED` lifecycle. |
| `created_at`, `updated_at`, `last_accessed_at`, `expires_at` | Existing lifecycle and retrieval bookkeeping. |

The existing `agent_memories` PostgreSQL table stores the same JSON metadata and lifecycle columns. The repository already supports scoped `save`, `search`, `touch`, `find_by_source`, and metadata filters. `MemoryManager.remember`, `supersede`, and `deactivate` are the canonical facade. Therefore the schema is sufficient for a minimal Semantic vertical slice; a second table, repository, or vector collection is not required.

### 1.2 Type compatibility risk

`MemoryType.PREFERENCE` intentionally uses the storage value `SEMANTIC`. In practice, `MemoryType.SEMANTIC` and `MemoryType.PREFERENCE` cannot be distinguished by the database `memory_type` column alone. A Semantic implementation must therefore use an explicit metadata contract, for example:

```text
memory_contract = SEMANTIC_V1
memory_role     = stable_fact
```

Existing Preference records are identified by their `preference_type`/`value` shape and Preference contract. A future canonical retriever must filter Semantic and Preference contracts explicitly inside the same retriever. A type-only query is unsafe and could mix the two domains.

### 1.3 Preference path

`PreferenceMemoryExtractor` is a completed-turn classifier. It recognizes a small deterministic vocabulary of explicit durable preferences and writes `MemoryType.PREFERENCE` records with `preference_type`, `value`, confidence, and source provenance. It does not currently define Semantic facts.

`MemoryUserPreferenceProvider` and `PreferenceRetriever` are compatibility-facing Preference views. Production composition already injects the combined canonical `MemoryRetriever`; a Semantic slice must not create another retrieval path or make the Preference provider the Semantic truth owner.

### 1.4 Episodic path

Episodic V1 already uses the same `MemoryRecord`/`agent_memories` storage with `memory_type=EPISODIC` and `memory_contract=EPISODIC_V1`. Its candidate builder accepts only terminal, exactly joined, verified runtime evidence and its writer goes through `MemoryManager`. Semantic must preserve this boundary: one Episode is evidence of a past event, not evidence of a stable user fact.

### 1.5 Lifecycle and canonical retrieval

The existing lifecycle can represent fact replacement without deleting audit history:

```text
ACTIVE -> SUPERSEDED
ACTIVE -> INACTIVE
```

`MemoryManager.supersede` is already scoped by user and tenant. Its current merge logic is Preference-specific, so a Semantic implementation will need a deterministic fact-key conflict operation in the same canonical manager/repository boundary. It must not direct-write storage or let an LLM delete records.

The canonical read path is:

```text
MemoryRetriever
  -> one MemoryRelevanceGate
  -> bounded ContextBuilder
  -> Interpreter projection
```

`MemoryRetriever` supports memory-type/status/tenant/metadata filtering and the shared gate. `ContextBuilder` recalls only when the current turn has a command, goal, or target query and limits model-facing memory. It currently labels Preference and Episodic roles; Semantic will need one additional bounded role label, not a new prompt path.

## 2. Semantic Definition and Boundary

Semantic Memory means:

> A stable fact about the user or the user's long-term context that may be useful across future conversations.

It is not a user instruction, a single historical event, or current runtime state.

| Domain | Meaning | Example | Owner / storage contract |
|---|---|---|---|
| Preference | How the user wants the assistant or content to behave; often a choice or style rule. | “我喜欢 Java 内容写得深入。” / “I prefer deep Java articles.” | Preference policy; existing `PREFERENCE` storage alias plus Preference metadata. |
| Semantic | A stable user/context fact, normally explicit or strongly corroborated. | “我主要做 Java 后端，现在在学习 Agent。” / “I mainly do Java backend work and am learning Agent.” | Proposed `SEMANTIC_V1` contract in existing `MemoryRecord`. |
| Episodic | One verified past experience and its outcome. | “上次发布技术内容前，用户调整了标题和发布时间，随后发布成功。” | Existing `EPISODIC_V1` contract. |
| Current State | What is true for the active Task, Objective, Execution, Resource, Approval, or reconciliation flow now. | “当前正在发布一篇 Java 帖子。” | Task/Objectives, Durable Runtime, Resource/Approval truth; never Memory. |

The same words can belong to different domains depending on their meaning:

- “我喜欢 Java 内容” is a Preference.
- “我是 Java 后端开发者” is a Semantic fact.
- “我上次发布了一篇 Java 帖子” is an Episode.
- “当前正在发布 Java 帖子” is Current State.

An action verb or a topic alone is never enough to create a Semantic fact.

## 3. Semantic Fact Candidate Sources

### 3.1 V1 source: explicit user self-statement

The only V1 admission source should be an authenticated `user` conversation message whose wording explicitly asserts a durable fact about the user or long-term context. The current Conversation/API path already carries user role, conversation scope, message content, and conversation identity; the Semantic source adapter should retain a stable message identity or content hash in internal provenance.

The source authority is the user's statement, not an LLM's belief about the user. A structured LLM extractor may propose fields in a later implementation, but the admission boundary must require an explicit source span/claim and must reject unsupported inference.

V1 allowlisted assertion families:

- primary work domain: `occupation_domain`;
- long-term learning domain: `learning_domain`.

### 3.2 Reserved future source: repeated stable evidence

Repeated evidence may be considered in a later version, but it must not be implemented as complex automatic inference in this slice. Multiple post topics, tool calls, Tasks, Episodes, or runtime states do not independently establish a Semantic fact.

### 3.3 Forbidden sources

Do not construct a Semantic candidate directly from:

- one Tool call, ToolResult, or Java response;
- one post topic or one content request;
- a current Task, Objective, Execution, Run, Resource, or Approval state;
- one Episodic Memory record;
- Conversation summary without an attributable explicit user assertion;
- LLM intermediate text or an LLM-only identity inference;
- runtime/business/resource IDs.

## 4. SemanticCandidate Contract

The minimum contract is:

```text
SemanticCandidate
  user_id
  tenant_id
  subject
  predicate
  object
  normalized_fact
  confidence
  source_type
  provenance
  observed_at
```

V1 constraints:

| Field | V1 rule |
|---|---|
| `user_id`, `tenant_id` | Required, authenticated, and immutable for the write. |
| `subject` | `user` only; do not build a general graph. |
| `predicate` | Allowlist only: `occupation_domain` or `learning_domain`. |
| `object` | Normalized bounded value such as `java_backend` or `agent`; no IDs. |
| `normalized_fact` | Short human-readable fact, e.g. “The user’s primary work domain is Java backend development.” It must not be a Task snapshot or quote raw payload. |
| `confidence` | Source/policy confidence, not unconstrained model confidence. Explicit statements may pass a high threshold; ambiguous statements do not. |
| `source_type` | V1: `USER_EXPLICIT_SELF_STATEMENT`; correction can be reserved as `USER_EXPLICIT_CORRECTION`. |
| `provenance` | Internal audit only: message/conversation reference, claim index or hash, source role, policy version. It is not model-facing fact text. |
| `observed_at` | Original message observation time; repository write time must not replace it. |

The candidate itself is not a database record and cannot write storage. It should have an allowlist/extra-field rejection boundary so a caller cannot smuggle Task, Execution, Run, Resource, or approval state into the fact.

### 4.1 Existing-record mapping

The candidate can be represented without schema change:

```text
MemoryRecord.memory_type       = SEMANTIC
MemoryRecord.content           = normalized_fact
MemoryRecord.structured_metadata = {
    memory_contract: SEMANTIC_V1,
    memory_role: stable_fact,
    subject: user,
    predicate: occupation_domain | learning_domain,
    object: java_backend | agent,
    normalized_fact: normalized_fact,
    observed_at: observed_at,
    provenance: internal_only,
}
MemoryRecord.source_type        = USER_EXPLICIT_SELF_STATEMENT
MemoryRecord.source_id          = stable source identity
MemoryRecord.task_id            = None
```

Because `SEMANTIC` is also the existing Preference storage value, `memory_contract=SEMANTIC_V1` and `memory_role=stable_fact` are mandatory discriminators. The model-facing projection should expose the fact and role, not internal provenance.

## 5. Fact Admission Policy

The policy has three outcomes; `UNKNOWN` is write-disabled and therefore effective `DROP`.

### KEEP

Keep only when all conditions hold:

- the authenticated user explicitly states the fact about themself or their durable context;
- the predicate and object are in the V1 allowlist;
- the fact is stable enough to be useful across conversations;
- the statement is not merely a preference, a current task, or a past event;
- it contains no sensitive value that this product has not explicitly authorized for long-term storage;
- scope, provenance, observation time, and normalized wording are present.

### DROP

Drop:

- temporary/current state (“I am publishing this post now”);
- a one-time action or one post topic;
- an inference from one Episode, Tool call, or execution;
- LLM-generated identity guesses;
- credentials, secrets, financial/health/legal details, or other sensitive data outside an explicit product policy;
- resource/runtime identifiers such as `post_id`, `draft_id`, `run_id`, `execution_id`, `schedule_id`;
- facts outside the V1 predicate/object allowlist.

### UNKNOWN

Return `UNKNOWN` for:

- ambiguous wording such as “I use Java,” which could mean a tool choice or an occupation;
- conflicting facts in one statement;
- a summary with no attributable user assertion;
- a fact whose time horizon is unclear;
- a candidate whose source role or observation time cannot be verified.

`UNKNOWN = DROP` in V1. The default is to remember less rather than guess a user fact.

## 6. Conflict and Update Policy

### 6.1 Conflict identity

For the two V1 predicates, the active truth key is:

```text
(tenant_id, user_id, subject, predicate)
```

The normalized object identifies the value of that fact. V1 treats each key as one primary active projection. Multi-valued occupations or learning domains are out of scope and should be `UNKNOWN` rather than silently creating multiple active truths.

### 6.2 Lifecycle transitions

```text
new explicit fact
  -> canonical fact-key lookup
  -> same value: merge evidence / idempotent update
  -> new value: supersede old ACTIVE fact, write new ACTIVE fact
```

The old record remains `SUPERSEDED` for audit and is excluded from normal retrieval. If supported by the existing metadata boundary, the old record may retain `replacement_memory_id`; it must not remain an active competing truth.

The update operation must be serialized or transactionally guarded in the canonical MemoryManager/Repository so concurrent turns cannot leave two active values for the same key. If the existing synchronous facade cannot provide this guarantee, extend that canonical boundary before enabling Semantic writes; do not add a second repository.

### 6.3 Authority precedence

```text
new explicit correction
  > new explicit self-statement
  > future repeated verified evidence
  > inferred evidence (never admitted in Semantic V1)
```

An explicit correction supersedes an older explicit fact for the same key. A newer explicit fact supersedes an older inferred fact. No LLM may freely delete or supersede a fact without a policy-approved explicit source.

### 6.4 Idempotency

Use two deterministic identities:

- source identity: stable message ID, or a bounded hash of conversation ID + normalized source message + claim index;
- semantic fact identity: scope + subject + predicate + normalized object.

Replaying the same message/claim must not add another active row. Repeating the same fact in a later message should merge evidence into the same active fact projection. A changed object must not collapse into the old object; it creates the replacement projection and supersedes the old one.

## 7. Retrieval Boundary

Semantic retrieval must reuse the existing canonical path:

```text
Current Turn
  -> MemoryRetriever
  -> MemoryRelevanceGate
  -> bounded ContextBuilder
  -> Interpreter
```

No `SemanticRetriever -> Prompt` path is allowed.

The single `MemoryRetriever` should:

- require `user_id` and `tenant_id` in production;
- request `ACTIVE` records only;
- distinguish `SEMANTIC_V1` from Preference using metadata contract/role, because the storage type aliases collide;
- apply the existing shared relevance/confidence gate;
- allow zero results;
- cap total memory items and fact length under the existing ContextBuilder budget;
- exclude superseded, legacy, ambiguous, or uncontracted Semantic rows.

The minimal model-facing distinction is a role on bounded recalled items:

```text
preference                 -> Preference
stable_fact                -> Relevant Fact
relevant_past_experience   -> Relevant Past Experience
```

These are presentation labels over one canonical recalled-memory collection, not three retrievers or three prompt injection paths. An unrelated request such as “查一下最近帖子” must not inject Java/Agent facts when the shared gate finds no relevant terms.

## 8. One Minimal Vertical Slice

### Scenario: explicit long-term technical background

One user turn:

```text
我主要做 Java 后端，现在在学习 Agent。
I mainly do Java backend work and am learning Agent.
```

This is one vertical slice with at most two independently validated Semantic candidates:

| Candidate | Predicate | Object | Normalized fact |
|---|---|---|---|
| A | `occupation_domain` | `java_backend` | The user’s primary work domain is Java backend development. |
| B | `learning_domain` | `agent` | The user is learning Agent development. |

Both are Semantic facts because the user explicitly self-identifies stable background/context. Neither is a Preference (“I prefer Java”) or an Episode (“I published Java content last time”). The `now/currently learning` wording is treated as long-term learning context for this narrow slice, with `observed_at` retained so a later explicit update can supersede it; it is not used as an active Task state.

### Cross-session behavior

For a later conversation:

```text
给我推荐一个 Agent 项目
```

the relevant `learning_domain=agent` fact may pass the canonical gate. For:

```text
查一下最近帖子
```

the Semantic gate should return no facts. A single Episode about a prior Java post must not create either Semantic fact.

## 9. Focused Evaluation Design

This design calls for a small deterministic benchmark; it does not call for the full Memory Evaluation matrix.

| Fixture | Expected result | Metric covered |
|---|---|---|
| Explicit self-statement above | Two admitted facts, no extra facts | Explicit fact extraction precision |
| “I use Java” without work/learning assertion | `UNKNOWN` / zero writes | Unsupported inference rate |
| One post topic, Tool call, Task, or Episode mentioning Java | Zero Semantic writes | Unsupported inference and Episode/Semantic confusion |
| Same source message replayed | One active fact per value | Duplicate fact rate |
| Explicit update from learning Java to learning Agent | New Agent fact `ACTIVE`; old Java fact `SUPERSEDED` | Supersede correctness |
| Relevant cross-conversation Agent project query | Agent fact returned | Relevant retrieval recall/precision |
| Unrelated recent-posts query | Zero Semantic facts | No-match false return rate |
| Explicit “I prefer Java” | Preference only | Preference/Semantic confusion rate |
| Verified publication Episode | Episodic only | Episode/Semantic confusion rate |
| Other user and other tenant with same facts | Zero out-of-scope results | User/tenant leakage |
| Superseded fact query | Superseded fact not returned | Lifecycle correctness |

Target direction:

- explicit candidate precision: `1.00` on eligible fixtures;
- unsupported inference rate: `0`;
- duplicate active fact rate: `0`;
- supersede correctness: `1.00` for deterministic replacement fixtures;
- relevant retrieval recall and precision: measured separately under the shared gate;
- no-match false return rate: `0`;
- Preference/Semantic and Episode/Semantic confusion: `0`;
- user and tenant leakage: `0`.

The benchmark must assert both candidate output and stored/retrieved records. Repository-only tests are insufficient because ContextBuilder and the common RelevanceGate are part of the contract.

## 10. Protected Boundaries and Current Risks

This design does not modify:

- ActionLoop semantics;
- Durable Runtime or Task/Objectives lifecycle;
- `RESULT_UNKNOWN` or reconciliation;
- MCP, Search, RAG, or Java business truth;
- the existing legacy Memory Runtime.

The existing private `RuntimeAgentService` legacy memory helpers remain a cleanup candidate, but repository search shows no current caller. They must not be wired as Semantic source, writer, or recall path. Existing Preference compatibility helpers likewise remain Preference-only.

### Blockers / implementation gates

There is no schema blocker for the design. Before implementation is enabled, these gates must be proven:

1. **Alias isolation:** because Preference and Semantic share the `SEMANTIC` storage value, all Semantic reads and writes must require `SEMANTIC_V1` metadata and a `stable_fact` role; a type-only query is not acceptable.
2. **Explicit source identity:** the ingestion boundary must preserve user role, scope, observed time, and stable message/claim provenance. A free-floating LLM extraction is not a source.
3. **Atomic fact replacement:** same-key updates must guarantee one `ACTIVE` fact. The canonical MemoryManager/Repository may be extended, but a second store is prohibited.
4. **Legacy quarantine:** old `SEMANTIC` rows without the Semantic contract must remain classified as Preference/legacy/unknown until explicitly migrated; they must not be guessed into Semantic facts.

If any gate cannot be satisfied within the canonical Memory boundary, Semantic implementation is blocked. No fallback to RuntimeAgentService or a parallel retriever is allowed.

## 11. Next Implementation Scope

When the design is accepted, the next implementation should contain only:

1. an explicit-user-source adapter and the narrow `SemanticCandidate` contract;
2. V1 admission policy for `occupation_domain` and `learning_domain`;
3. canonical fact-key idempotency and transactional/safely serialized supersede behavior through MemoryManager/Repository;
4. one canonical `MemoryRetriever` contract filter and one bounded `stable_fact` ContextBuilder role;
5. the focused benchmark above, including scope isolation and no-match cases.

It must not add an episode table, Semantic table, vector collection, second repository, second retriever, second relevance policy, or new Semantic/Procedural/episodic scenarios. It must not modify the protected runtime/business boundaries.

`SEMANTIC_MEMORY_DESIGN_COMPLETE`
