# Testing

Run the repository checks from the root unless a command says otherwise:

```powershell
uv run pytest -q
uv run pytest --collect-only -q
uv run ruff check packages/agent_core apps/agent_api apps/agent_worker services/greenbook_mcp packages/contracts packages/security packages/java_client tests
uv run python -m compileall -q packages apps services
uv lock --check
docker compose config
git diff --check
```

Java:

```powershell
cd apps/backend
mvn test
```

Frontend:

```powershell
cd zhiguang-fe
npm run lint
npm run build
```

The root E2E smoke path is `scripts/smoke-test.ps1`; the live flow is
`scripts/e2e-test.ps1` and requires the local services and a configured E2E
account.
