"""Measure snapshot versus scoped provider context without calling a provider."""

from __future__ import annotations

import asyncio
import copy
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
for _path in (
    ROOT,
    ROOT / "packages" / "agent_core",
    ROOT / "packages" / "contracts",
    ROOT / "packages" / "evaluation",
    ROOT / "packages" / "security",
    ROOT / "apps" / "agent_api",
):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from greenbook_agent_core.command.models import CommandContext
from greenbook_agent_core.context.models import ContextSnapshot
from greenbook_agent_core.context.projection import project_interpreter_context
from greenbook_agent_core.turn import ContextAssembler

from evaluation.semantic_longtail.run_benchmark import _context_for


class _FrozenSnapshotBuilder:
    def __init__(self, snapshot: ContextSnapshot) -> None:
        self._snapshot = snapshot

    async def build(self, **_kwargs: Any) -> ContextSnapshot:
        return self._snapshot


async def main() -> None:
    dataset = json.loads(
        (ROOT / "evaluation" / "semantic_longtail" / "cases.json").read_text(
            encoding="utf-8"
        )
    )
    rows: list[dict[str, Any]] = []
    for case in dataset.get("cases") or []:
        messages = [str(case.get("message") or "")]
        if case.get("category") == "H_PARAPHRASE":
            messages.extend(str(item) for item in case.get("variants") or [])
        for message in messages:
            payload = copy.deepcopy(
                _context_for(case, dataset.get("context_library") or {})
            )
            old_context = CommandContext.model_validate(payload)
            old_chars = len(
                json.dumps(
                    project_interpreter_context(old_context),
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            )
            snapshot = ContextSnapshot.model_validate(payload)
            assembled = await ContextAssembler(
                _FrozenSnapshotBuilder(snapshot)
            ).assemble(
                conversation_id=str(snapshot.conversation_id),
                user_id=str(snapshot.user_id),
                tenant_id=str(snapshot.tenant_id),
                user_input=message,
            )
            new_chars = len(
                json.dumps(
                    project_interpreter_context(assembled.to_command_context()),
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            )
            rows.append({"case_id": case["id"], "old": old_chars, "new": new_chars})
    old_values = [row["old"] for row in rows]
    new_values = [row["new"] for row in rows]
    print(json.dumps({
        "utterances": len(rows),
        "old_avg_chars": round(sum(old_values) / len(old_values), 2),
        "new_avg_chars": round(sum(new_values) / len(new_values), 2),
        "old_max_chars": max(old_values),
        "new_max_chars": max(new_values),
        "old_total_chars": sum(old_values),
        "new_total_chars": sum(new_values),
    }, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
