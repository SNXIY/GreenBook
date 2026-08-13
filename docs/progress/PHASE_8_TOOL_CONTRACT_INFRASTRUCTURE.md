# Phase 8 Tool Contract Infrastructure

## Scope

This phase makes the Runtime MCP registry the single handler contract
boundary.  It does not add a new capability or change Planner, Worker,
ExecutionStateManager, or ToolRuntime lifecycle behaviour.

## Canonical contract

`greenbook_contracts.tool_contract.ToolContract` now carries:

- Pydantic `input_schema` and `output_schema`
- capability and handler identity
- operation mapping (`CREATE_CONTENT`, `UPDATE_CONTENT`, `SCHEDULE_PUBLISH`, etc.)
- `PermissionPolicy` (risk and approval)
- `RetryPolicy` (bounded attempts and retryable error codes)
- `SideEffectMetadata` (external systems, idempotency, destructive writes)

`greenbook_mcp_server.tool_registry.ToolDefinition` is the concrete MCP
registration of that shared contract.  The old `argument_model` accessor is
retained as a read-only compatibility alias, while every active tool must
declare an input model.

## Active Runtime coverage

The registry now declares schemas and metadata for all 16 active MCP tools:

| Domain | Tools |
| --- | --- |
| Community | `search_public_posts`, `get_post`, `list_own_posts` |
| Content | `create_draft`, `get_draft`, `list_drafts`, `revise_draft` |
| Publication | `schedule`, `get_status`, `update_schedule`, `cancel_schedule`, `publish_now` |
| Interaction | `list_comments`, `send_reply` |
| Analytics | `get_post_performance`, `get_account_summary` |

`content.create_draft` now exposes `title` + `instruction`; generated
`content` is Creator output, not a handler input.  Capability validation checks
that single-tool capability fields and handler schemas remain identical while
allowing intentionally composite capabilities.

## Runtime integration

MCP tool discovery exports input/output schemas and all policy metadata.  The
server validates both input and output envelopes, and fails before/after the
downstream call with an explicit validation result rather than reporting a
successful step.

## Deliberate non-coverage

`DELETE_CONTENT`/post deletion is not an active Runtime MCP handler in this
workspace.  Java has a legacy Assistant capability for post deletion, but no
Runtime capability, handler, or Planner mapping exists.  Phase 8 therefore
does not invent a fake contract or silently route through Legacy; adding that
operation requires a separate capability and business-handler decision.

## Verification

- `tests/unit/test_tool_contract_infrastructure.py`
- `tests/unit/test_revision_tool_contract.py`
- `tests/unit/test_argument_binding_generate_content.py`
- `tests/unit/test_capability.py`
- `python -m compileall packages services apps`
- `git diff --check`
