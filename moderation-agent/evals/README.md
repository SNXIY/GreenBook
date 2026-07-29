# Moderation Evaluation Data

This directory contains offline evaluation data for the moderation agent. Each
line is one UTF-8 JSON object conforming to
[`schema/moderation-eval-case-v1.schema.json`](schema/moderation-eval-case-v1.schema.json).
The authoritative Python model is `evals.moderation.schemas.ModerationEvalCase`.

## Dataset Lifecycle

1. `candidates/`: policy-template, model-generated, or curated records. Labels
   have status `PROPOSED` and must not be reported as human gold.
2. Human review: reviewers check the content, context, risk, action, evidence
   offsets, Policy codes, privacy declaration, and scenario grouping.
3. `golden/`: only records with status `REVIEWED` or `ADJUDICATED`, pseudonymous
   reviewer IDs, and an assigned split may enter this directory.
4. Calibration and test records from the same `scenario_group_id` must never be
   separated across splits.

The initial seed file is
[`candidates/seed-v1.proposed.jsonl`](candidates/seed-v1.proposed.jsonl). It has
100 pre-labeled, machine-validated cases in 25 four-case scenario groups. It is
deliberately marked `PROPOSED`; no real reviewer has yet confirmed it.

## Record Contract

Important fields:

| Field | Purpose |
| --- | --- |
| `case_id` | Unique, stable record ID |
| `scenario_group_id` | Keeps minimal pairs and related variants together |
| `revision` | Increments when an input or label changes |
| `split` | `UNASSIGNED`, `DEVELOPMENT`, `CALIBRATION`, `TEST`, or `CHALLENGE` |
| `input` | Current content plus parent, conversation, author-history, and report context |
| `label` | Risk, expected/acceptable actions, Policy codes, evidence spans, and reason |
| `annotation` | Origin, review status, reviewer IDs, agreement, and adjudication |
| `privacy` | No-sensitive-data, synthetic-only, or production-redacted declaration |
| `policy_snapshot` | Version and SHA-256 fingerprint of every referenced Policy |

Evidence offsets use Python/Unicode code-point indexes. The validator checks
that every `text` value exactly equals its source slice.

## Commands

Set the import path once in PowerShell:

```powershell
Set-Item Env:PYTHONPATH "src"
```

Rebuild the deterministic seed data and JSON Schema:

```powershell
.venv\Scripts\python.exe scripts\build_moderation_eval_seed.py
.venv\Scripts\python.exe scripts\export_moderation_eval_schema.py
```

Validate candidate data:

```powershell
.venv\Scripts\python.exe scripts\validate_moderation_eval_dataset.py `
  evals\candidates\seed-v1.proposed.jsonl
```

Validate a human-reviewed release as gold:

```powershell
.venv\Scripts\python.exe scripts\validate_moderation_eval_dataset.py `
  evals\golden\moderation-v1.jsonl --gold
```

`--gold` fails if any record is unreviewed or has an unassigned split. Exact
duplicates always fail. Cross-scenario character-trigram duplicates at or above
the configured threshold fail by default; same-scenario near variants are
reported separately.

Generate new candidates from the built-in Policy set:

```powershell
.venv\Scripts\python.exe scripts\generate_moderation_eval_candidates.py `
  --output evals\candidates\generated-v1.proposed.jsonl `
  --per-policy 8 --model gpt-5-mini
```

Use `--policy-source database` to read enabled policies from the moderation
database, `--policy-code ADV-001` to filter, and `--max-concurrency` to bound
parallel live-model calls. Model output is
validated and remains `PROPOSED`; it can never generate gold data directly.

## Human Confirmation

For one-reviewer confirmation:

1. Confirm or correct every `label` field and evidence span.
2. Set `annotation.status` to `REVIEWED`.
3. Add a pseudonymous ID such as `reviewer-017` to
   `annotation.reviewer_ids`.
4. Assign the whole scenario group to one dataset split.
5. Run validation with `--gold`.

For disputed or high-impact cases, keep two or more independent reviewer IDs,
set an independent `adjudicator_id`, record agreement, and use status
`ADJUDICATED`. Do not put names, email addresses, employee IDs, or raw
production content in annotation metadata.

## Privacy Rules

Synthetic PII is allowed only for `POLICY_TEMPLATE`, `LLM_GENERATED`, and
`CURATED_SEED` sources, must use privacy mode `SYNTHETIC_ONLY`, and every
detected value must be listed exactly in `synthetic_sensitive_values`.
Production-derived sources may not use this escape hatch: phone numbers,
identity numbers, email addresses, common obfuscations, and plaintext
credentials must already be redacted. Validation errors contain only masked
values or short hashes.
