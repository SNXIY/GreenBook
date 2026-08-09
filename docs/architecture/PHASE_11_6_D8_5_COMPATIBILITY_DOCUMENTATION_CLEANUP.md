# Phase 11.6-D8.5 Compatibility Documentation Cleanup

## Classification

The active History compatibility path is:

```text
packages/assistant_core/greenbook_assistant_core/compatibility/history/
tests/compat/history/
```

It contains `RunExecutionLink`, `RunExecutionAdapter`,
`ExecutionReference`, and the link repositories. The former Runtime
compatibility package and its old test path are retired.

ACTIVE application and migration documentation uses `compatibility/history`
and `tests/compat/history`. No active code, test, script, README, or CI
reference uses the retired paths.

## Historical references

Older Phase reports and audit records under `docs/architecture/` and
`docs/archive/` may retain former compatibility paths as historical
descriptions of the system at that time.
Those records are not rewritten because changing their conclusions would
reduce their value as an audit trail.

## Cleanup

The obsolete runtime compatibility export file was removed. The old test
directory was already absent after the D8.4 test move. Runtime execution
components remain separate from History compatibility; no Runtime state
component or history persistence table was removed.

## Verification contract

The cleanup is verified by scanning for active references, running the History
compatibility and projection tests, compiling the affected Python packages,
and checking the Git diff for whitespace errors.
