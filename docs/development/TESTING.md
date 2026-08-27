# Testing

GreenBook uses focused -> affected-family -> broad regression. Do not run the
entire Browser matrix for an isolated code change, and do not use API-only
checks as a substitute for a Browser acceptance case.

## Fast local checks

Run repository checks from the root unless a command says otherwise:

```powershell
uv run pytest -q
uv run pytest --collect-only -q
uv run ruff check packages apps services tests
uv run python -m compileall -q packages apps services scripts
uv lock --check
docker compose config
git diff --check
```

For the current final stabilization scope, the accepted broad evidence is the
`.venv` audit (`1602 passed, 0 failed, 1 cache warning`). Reuse that evidence
when code has not changed; otherwise run the affected suite first.

## Focused suites

```powershell
uv run pytest -q tests/unit/test_command_runtime.py tests/unit/test_action_loop_parallel_objectives.py
uv run pytest -q tests/unit/test_objective_completion_owner.py tests/unit/test_real_mcp_boundary.py
uv run pytest -q tests/unit/test_reliable_execution_wiring_phase4b1.py tests/unit/test_memory_retriever.py
```

Frontend contract/projection checks are in `zhiguang-fe/package.json`:

```powershell
Set-Location zhiguang-fe
npm run lint
npm run test:agent-ux
npm run test:user-activity
npm run test:execution
npm run test:conversation-lifecycle
npm run test:semantic-confirmation
npm run build
```

## Java and Browser E2E

```powershell
Set-Location apps/backend
mvn test
```

The root smoke path is `scripts/smoke-test.ps1`; the live path is
`scripts/e2e-test.ps1` and requires local services and a dedicated USER E2E
account. The final UX smoke is `tests/e2e/browser_ux_final_smoke.py` and begins
with real Frontend textarea input.

Performance harnesses write runtime artifacts and may create local test
drafts/schedules. Keep those artifacts separate from production data and never
approve a destructive action automatically.
