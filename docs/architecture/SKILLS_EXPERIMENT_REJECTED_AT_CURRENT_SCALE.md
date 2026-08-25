# Skills Progressive Disclosure Experiment

Status: `SKILLS_EXPERIMENT_REJECTED_AT_CURRENT_SCALE`

Date: 2026-08-25

## Scope

The Phase 2C lightweight Skills experiment evaluated progressive disclosure
around the existing GreenBook semantic and execution path. It was not merged
into production. GreenBook currently has 14 active MCP Tools and 16 canonical
capabilities; the experiment was evaluated at that scale only.

## Evidence

The focused Phase 2D dataset contained 50 natural-language scenarios covering
chat, Search, Content Authoring, Publication, Content Management, negative
requests, explicit targets, relative time, draft-only intent, and multi-Skill
requests.

| Metric | Skills OFF | Skills ON |
| --- | ---: | ---: |
| Required capability recall | 100% | 92% |
| Skill recall | — | 84.33% |
| Skill precision | — | 81% |
| Unnecessary Skill load | — | 14% |
| Missed Skill rate | — | 18% |
| Total model context | 198,473 chars | 215,273 chars |

Skills ON increased the measured total model input by 16,800 chars, or
`+8.465%`, across the eight representative call-site samples. Selection
latency was small, but did not offset the context increase or the capability
recall loss.

## Rejection Rationale

The experiment was rejected for the current product scale because:

1. The existing 14 Tools / 16 capabilities do not justify a second disclosure
   layer yet.
2. Raw trigger matching missed relevant domains and loaded irrelevant domains.
3. Incorrect positive Skill activation could narrow the canonical capability
   catalog and reduce required capability recall from 100% to 92%.
4. The always-present catalog plus loaded guidance increased final model input
   instead of reducing it.

This is an evaluation result, not a production failure. The canonical MCP
boundary, CapabilityRegistry, ActionLoop, Qualification, Durable Runtime,
ToolRuntime, and semantic/resolution invariants remain the accepted path.

## Revisit Triggers

Reconsider progressive disclosure only when evidence shows at least one of the
following:

- active Tool/capability count grows enough that the all-capability schema is a
  measured context or selection bottleneck;
- a real benchmark shows a quality or safety gain that exceeds catalog and
  instruction context cost;
- Skill activation can consume existing structured semantic facts without
  introducing a second semantic interpreter;
- capability filtering is fail-open for uncertain Skill matches and cannot
  reduce required capability recall;
- a new evaluation demonstrates lower total model input and no regression in
  required capability, argument, Tool, or write safety metrics.

No Skills runtime, Skill environment flag, Skill registry, or Skill tests are
kept in the active production tree.
