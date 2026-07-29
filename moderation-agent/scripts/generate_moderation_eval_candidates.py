r"""Generate proposed moderation evaluation candidates from active policies.

Run from the repository root:
    $env:PYTHONPATH="src"
    .venv\Scripts\python.exe scripts\generate_moderation_eval_candidates.py `
        --output evals/candidates/generated.jsonl --per-policy 8
"""

import argparse
import asyncio
from datetime import UTC, datetime

from pydantic import TypeAdapter

from core.settings import settings
from database import DatabaseManager
from evals.moderation.dedup import validate_duplicates
from evals.moderation.generator import PolicyCandidateGenerator
from evals.moderation.io import validate_dataset, write_jsonl
from evals.moderation.policy_snapshot import PolicyDefinition
from evals.moderation.privacy import validate_privacy
from moderation.schemas import ModerationPolicyCreate, RiskType
from moderation.services.policies import DEFAULT_POLICIES, ModerationPolicyService
from schema.models import AllModelEnum

_MODEL_ADAPTER: TypeAdapter[AllModelEnum] = TypeAdapter(AllModelEnum)


async def _load_database_policies() -> list[PolicyDefinition]:
    database = DatabaseManager()
    await database.start(settings.moderation_database_url(), create_schema=False)
    try:
        policies = await ModerationPolicyService(database).list(enabled_only=True)
        return [PolicyDefinition.from_policy(policy) for policy in policies]
    finally:
        await database.close()


def _load_default_policies() -> list[PolicyDefinition]:
    policies: tuple[ModerationPolicyCreate, ...] = DEFAULT_POLICIES
    return [PolicyDefinition.from_policy(policy) for policy in policies]


async def main(args: argparse.Namespace) -> None:
    policies = (
        await _load_database_policies()
        if args.policy_source == "database"
        else _load_default_policies()
    )
    if args.policy_code:
        selected = set(args.policy_code)
        policies = [policy for policy in policies if policy.code in selected]
        missing = selected - {policy.code for policy in policies}
        if missing:
            raise ValueError(f"Unknown or disabled policy codes: {', '.join(sorted(missing))}")
    normal_policies = [policy.code for policy in policies if policy.risk_type == RiskType.NORMAL]
    if args.policy_code and normal_policies:
        raise ValueError(
            "NORMAL policies are not direct generation targets; safe exclusions are generated "
            f"for each risk policy instead: {', '.join(normal_policies)}"
        )
    policies = [policy for policy in policies if policy.risk_type != RiskType.NORMAL]
    if not policies:
        raise ValueError("No policies were selected")

    model_name = _MODEL_ADAPTER.validate_python(args.model or settings.DEFAULT_MODEL)
    generator = PolicyCandidateGenerator.from_model_name(model_name)
    cases = await generator.generate(
        policies,
        per_policy=args.per_policy,
        batch_id=args.batch_id,
        language=args.language,
        max_concurrency=args.max_concurrency,
    )
    validate_dataset(cases)
    privacy_report = validate_privacy(cases)
    duplicate_report = validate_duplicates(cases, near_threshold=args.near_threshold)
    write_jsonl(args.output, cases)

    print(
        f"Wrote {len(cases)} PROPOSED candidates from {len(policies)} policies "
        f"to {args.output}"
    )
    print(
        f"Privacy warnings: {len(privacy_report.warnings)}; "
        f"intentional near variants: {len(duplicate_report.intentional_variants)}"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, help="Destination JSONL path")
    parser.add_argument(
        "--policy-source",
        choices=("defaults", "database"),
        default="defaults",
        help="Read built-in defaults or enabled policies from the moderation database",
    )
    parser.add_argument(
        "--policy-code",
        action="append",
        default=[],
        help="Generate only this policy code; repeat the flag to select several",
    )
    parser.add_argument("--per-policy", type=int, default=8)
    parser.add_argument("--language", default="zh-CN")
    parser.add_argument("--max-concurrency", type=int, default=4)
    parser.add_argument("--near-threshold", type=float, default=0.88)
    parser.add_argument(
        "--batch-id",
        default=datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ"),
        help="Stable identifier embedded in case and scenario IDs",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="Model enum value; defaults to DEFAULT_MODEL from settings",
    )
    asyncio.run(main(parser.parse_args()))
