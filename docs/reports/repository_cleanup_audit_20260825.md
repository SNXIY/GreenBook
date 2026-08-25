# REPOSITORY_CLEANUP_AUDIT

Date: 2026-08-25 (Asia/Shanghai)

Scope: repository hygiene after the verified Reliable Agent Harness baseline. No
L1/L2/L3, semantic, safety, or performance matrix was rerun. No production
source or runtime contract was removed. The pre-existing untracked
`.claude/settings.local.json` was preserved.

## BASELINE

- branch: `chore/repository-cleanup-20260825`
- baseline commit: `018082810249c9b3efd7f610bb0baf5aa53699b1`
- immutable tag: `greenbook-reliable-agent-baseline-20260825`
- tag message: `Reliable Agent Harness baseline after L1/L2/L3, evaluation and real process restart recovery.`
- remote: `origin https://github.com/SNXIY/GreenBook.git` (fetch and push)
- the tag was pushed before cleanup; the cleanup branch was created from the tagged commit

## INVENTORY

The baseline contained the following tracked areas (counts were obtained with
`git ls-files`; generated `artifacts/` are tracked by the baseline):

- production/runtime: `apps/`, `packages/`, `services/`, `contracts/`, `infra/`, and `zhiguang-fe/src/`
- tests: `tests/`, `zhiguang-fe/tests/`, Java tests under `apps/backend/src/test/`
- evaluation: `evaluation/`, `artifacts/`, and the current evaluation scripts
- docs: `docs/`, `README.md`, `PROJECT_CONTEXT.md`
- operations: `scripts/`, `.github/workflows/`, `pyproject.toml`, `uv.lock`, `docker-compose.yml`
- archived/legacy areas: `docs/archive/`, architecture history, compatibility adapters,
  and retired product compatibility surfaces

The working tree also contained approximately 993 MB of `.runtime` data,
approximately 62 MB of uv cache, approximately 81 MB of frontend working data,
and many pytest scratch trees. The largest runtime consumers were the active
CDP profile and the formal `round1-final-v2` evidence set.

## DELETE_SAFE

| path/group | reason | reference proof |
| --- | --- | --- |
| root `.pytest-tmp*`, `.pytest_tmp_residual_*`, `.tmp*`, `tmpgreenbook-full-p0*` | one-shot pytest basetemp, debug logs/scripts, and test output; reproducible and not production inputs | `git check-ignore -v`; `rg` found no active import, CI, or docs consumer; tracked residual files were generated output |
| root `.uv-cache/`, `.ruff_cache/`, root/source-tree `__pycache__/` | reproducible tool/compiler caches | ignored by `.gitignore`; no source or CI reference; imports use source/package paths |
| `.runtime/chrome-ux-profile*` except the active `edge-cdp-profile` | old UX browser profiles; no current process used them | process command-line scan found only `edge-cdp-profile`; exact-name `rg` scan returned no source/docs consumer |
| `.runtime/convergence-e2e/`, `e2e/`, `e2e-p0-case10*`, `focused-e2e/`, `golden-e2e/`, `golden-final-closure/`, `golden-p0-*`, `long-session-final-20260824/`, `tmp-inspect/` | generated browser/E2E output; harnesses recreate their output directories | `scripts/dev/phase_p0_e2e.py`, `scripts/dev/semantic_confirmation_e2e.py`, and `tests/e2e/golden_final_closure.py` are the producers; exact old output paths had no formal-report consumer |
| unreferenced root `.runtime/*.log` | historical process/debug logs, not runtime configuration | exact filename scan across README, docs, scripts, tests, evaluation, and runtime entrypoints; the four report/health-referenced logs were retained |
| `zhiguang-fe/dist/` | Vite production output | `zhiguang-fe/package.json` (`npm run build`); frontend `.gitignore`; build passed and the output was deleted after validation |
| `agentgreen-book.tmp-perf-p1-p4.py` | one-off profiling/debug script with no active consumer; it also contained machine-local test credentials | `rg` across source/docs/CI/scripts returned no reference; `git log` showed it was only part of the baseline first commit |

The first cleanup pass had 311 validated target paths and measured 395,758,820
bytes. Eight targets were not deleted because they were locked or ACL-blocked.
A post-test cleanup removed 443 additional `__pycache__` directories and the
frontend `dist/` directory.

## DELETED

- 303 of the first-pass generated/cache/E2E/runtime-log target paths, measured at
  approximately 395.6 MB after excluding the locked targets.
- 443 source/test-tree `__pycache__` directories and the regenerated frontend
  `dist/` output, measured at approximately 14.75 MB.
- The unreferenced tracked `agentgreen-book.tmp-perf-p1-p4.py` profiling residue
  was removed after a content and full-reference scan proved it had no consumer.
- The 20 tracked files under the four `.pytest_tmp_residual_*` directories are
  staged as generated test residue deletions; no production file is in the diff.
- Formal evidence was not deleted: `docs/reports/`, `.runtime/round1-final-v2/`,
  `.runtime/stable-baseline/`, and the retained semantic artifact sets remain.

## KEEP

- Production boundaries: ActionLoop, Durable Runtime, Task/Objectives,
  ContextAssembler/DerivedConversationContext, TargetResolver/TemporalResolver,
  Qualification, ToolRuntime, MCP, HITL, Semantic Confirmation,
  OperationLedger, Reconciliation, Queue/Worker, and Java Agent Facade.
- Regression/evaluation coverage, including continuation, crash/resume,
  idempotency, RESULT_UNKNOWN, approval, semantic confirmation, and failure-family tests.
- Formal reports and evidence:
  `docs/reports/l2_t13_projection_recovery_20260825.md`,
  `docs/reports/real_process_restart_recovery_20260825.md`,
  `docs/reports/overnight_final_report_20260825.md`,
  `.runtime/round1-final-v2/`, `.runtime/stable-baseline/`,
  `artifacts/overnight_semantic_20260825/`,
  `artifacts/semantic_longtail_20260822/`, and the final L1 artifact.
- `.runtime/edge-cdp-profile/`: active Edge/CDP process uses this path.
- `apps/backend/target/`: the running Java process uses `target/classes`.
- `zhiguang-fe/node_modules/`: the running Vite process uses the local install.
- `.runtime/agent-worker-health.json` and the logs linked by the environment/recovery reports.
- Current CI/build configuration, fixtures, browser harness source, and package manifests.

## ARCHIVE_CANDIDATE

These were not deleted because they have historical evaluation or architectural
review value, even where no active consumer was found. The proof scan was made
with `rg`, `git ls-files`, package manifests, CI, scripts, and docs.

- Older tracked benchmark/evaluation runs under `artifacts/`, including the
  `residual_*`, `retry_*`, `semantic_interpreter_repair_*`, duplicate
  `semantic_longtail_*`, `semantic_phase2_*`, and duplicate
  `semantic_pipeline_*` sets. Active exceptions were retained where the
  evaluator or formal reports reference them (`overnight_semantic_20260825`,
  `semantic_longtail_20260822`, `semantic_longtail_20260822_final`, the
  whitebox/replay inputs, and the final L1 artifact).
- Unreferenced root runtime JSON/JSONL diagnostic evidence such as
  `bug4-live-fault-evidence.jsonl`, convergence/interpreter traces,
  `e2e-p0-interpreter*.jsonl`, `l1-context-phase2-target-debug.jsonl`, and
  `inspect_task_state.py`. These are candidates for a deliberate evidence
  retention/archive policy, not automatic deletion.
- `docs/archive/**`, architecture phase reports, compatibility history, and
  retired Interaction/Moderation/Analytics compatibility surfaces. The source
  scan still found compatibility imports, tests, migrations, or historical
  documentation, so no source deletion was proven safe in this pass.

## BLOCKED

| path | missing/safety evidence |
| --- | --- |
| `.pytest_cache/` | Windows ACL denied recursive removal; not forced or taken over |
| `.tmp-pytest-actionloop-full/` | Windows ACL denied removal; directory is empty but was not forcibly taken over |
| `.runtime/20260825-postboot-frontend.{err,out}.log`, `.runtime/20260825-postboot-java.{err,out}.log`, `.runtime/20260825-t13-fixed-agent.{err,out}.log` | active start/runtime processes still hold file handles; services were not stopped just to remove logs |
| `.runtime/edge-cdp-profile/`, `apps/backend/target/`, `zhiguang-fe/node_modules/` | active canonical runtime consumers; deletion would affect the live harness |
| full-tree byte total | not reported as authoritative because active profiles, local dependency trees, and the ACL-locked paths are intentionally retained |

## .gitignore changes

Added:

- `**/.pytest_tmp*/` to prevent the tracked-residue naming variant from returning.
- `**/.claude/settings.local.json` for machine-local assistant permissions.

Existing rules already cover `.runtime/`, Python caches, uv/ruff/pytest caches,
logs, Node `node_modules/` and `dist/`, Java `target/`, and local temp trees.
No broad `*.json` or evidence-directory rule was added, so fixtures and formal
reports remain visible to Git.

## TEST RESULT

- Production imports: 12 core/runtime modules imported successfully.
- Python targeted core/recovery/context/tool-runtime/queue/ledger/reconciliation/
  projection suite: **162 passed** (`-p no:cacheprovider`).
- Frontend: `npm run lint` passed; agent UX, user activity, execution contract,
  and semantic-confirmation projection tests passed; `npm run build` passed.
- Java affected tests: **33 passed**, 0 failures, 0 errors across Agent Facade
  security/search, draft metadata, idempotency, scheduled publication state
  machine, and text-only post coverage.
- Integration smoke: Frontend `:5173`, Agent API `:8094`, and Java `:8080`
  returned HTTP 200. Canonical listeners were present on `25432`, `33306`,
  `26379`, `39092`, `26333`, and `26334`.
- Runtime recovery note: while deleting the ignored `.uv-cache`, the live
  WatchFiles reloader observed the cache churn and began a reload while the
  browser held SSE connections. The API was temporarily non-responsive; only
  the verified stuck Agent API/reloader processes were stopped and the original
  `scripts/start-agent.ps1` configuration was started again. `/health` returned
  200 after recovery. No source, database, queue, Java truth, or formal
  evidence changed.
- The expensive full L1/L2/L3, semantic, safety, and performance matrices were
  not rerun.

## SIZE BEFORE / AFTER

- tracked files: baseline HEAD **3585**; the cleanup diff removes **21** tracked
  residue files (20 pytest outputs plus the unreferenced profiling script); with
  this report added, the post-commit tree will contain **3565** tracked files.
- measured generated cleanup: approximately **395.6 MB** in the successful
  first pass plus **14.75 MB** of post-validation compiler/frontend output.
- full working-tree size: intentionally not stated as authoritative because
  active `.runtime/edge-cdp-profile`, local venv/node_modules, and blocked paths
  remain.
- cleanup target paths: 303 successful first-pass paths plus 444 regenerated
  cache/build paths; recursive directories contain more individual files.

## GIT DIFF SUMMARY

The intended tracked diff is limited to:

- `.gitignore` additions described above;
- deletion of the 20 tracked pytest residue files and the unreferenced profiling script;
- this audit report.

No production source, test source, CI, manifest, formal report, or baseline tag
was modified. The pre-existing untracked `.claude/` directory remains outside
the commit.

## VERDICT

**CLEANUP_PARTIAL**

The proven generated residue was removed and the canonical runtime/tests remain
healthy. Cleanup is partial because ACL/file-lock blockers remain and the
historical benchmark/evidence and legacy source areas require an explicit
retention/archive decision before further deletion.
