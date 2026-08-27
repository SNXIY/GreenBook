# GreenBook Final Browser Acceptance

Date: 2026-08-27
Branch: `feature/hybrid-search-rag`

## Result

`PASS_WITH_LIMITATIONS`

The retained Browser matrix covers the requested real Frontend entry path:
conversation lifecycle, search/read, draft, schedule/update/cancel, immediate
publish, search plus creation, RAG, two independent drafts, three-objective
semantics, HITL, rapid send, stale response, switch, and refresh.

The final UX smoke was run from the actual Browser UI after the TUF observer
was added. It passed:

- rapid double-send admitted exactly one Run, terminal `COMPLETED`;
- switching from Conversation A while it was running did not leak A into B;
- the original A response remained present after returning to A;
- no internal execution/operation/objective identifiers or raw error codes
  were observed in the rendered UX;
- Frontend projection, Agent Run state, Java state, and business truth had no
  observed mismatch in the retained write evidence.

## TUF / UX timing

TUF is defined as accepted request to the first meaningful user-facing
progress/activity/status text. The observer reads only real rendered DOM nodes
(`role=status` and activity/thinking/progress classes); it does not create
timers, fake events, or product states. Two controlled observations completed
the business Run but found no dedicated meaningful progress node within the
60-second observation window, so:

`TUF = UNAVAILABLE`

The existing visual progress checks remain PASS, but this timing result is not
invented from terminal completion or generic loading.

## Known limitation

RAG Browser cases admitted through `community.answer_from_knowledge` and
failed closed when evidence was insufficient. Full grounding/citation proof
remains `RAG_CURRENT_LIMIT_ACCEPTED_PARTIAL`; this is an accepted quality
limitation, not a new Browser correctness regression.

Evidence is indexed by
[final_system_evaluation_results.json](../evaluation/final_system_evaluation_results.json)
and the retained recovery worklog under
`docs/archive/recovery/overnight-20260826-27/`.
