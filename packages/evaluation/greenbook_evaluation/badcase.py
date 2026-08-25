"""BadCase model — a single failure record with classification."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from enum import StrEnum

from pydantic import BaseModel, Field


class FailureType(StrEnum):
    # Command understanding
    WRONG_CATEGORY = "WRONG_CATEGORY"
    WRONG_RELATION = "WRONG_RELATION"

    # Decomposition
    OVER_SPLIT = "OVER_SPLIT"
    UNDER_SPLIT = "UNDER_SPLIT"
    WRONG_DEPENDENCY = "WRONG_DEPENDENCY"

    # Reference
    WRONG_TASK = "WRONG_TASK"
    AMBIGUITY_MISSED = "AMBIGUITY_MISSED"

    # Execution
    WRONG_TOOL = "WRONG_TOOL"
    MISSING_ARTIFACT = "MISSING_ARTIFACT"
    RECOVERY_FAILED = "RECOVERY_FAILED"

    # Canonical evaluation stages used by the semantic and thin Runtime
    # evaluators.  The older fine-grained values above remain compatible with
    # historical evaluation reports.
    INTERPRETER = "INTERPRETER"
    TEMPORAL = "TEMPORAL"
    TARGET = "TARGET"
    CLARIFICATION = "CLARIFICATION"
    ACTIONLOOP = "ACTIONLOOP"
    TOOL = "TOOL"
    JAVA = "JAVA"
    PROJECTION = "PROJECTION"
    CONTINUATION = "CONTINUATION"
    OBSERVABILITY = "OBSERVABILITY"

    # Uncategorised
    UNKNOWN = "UNKNOWN"


class BadCaseStatus(StrEnum):
    """Lifecycle state for a triaged regression record."""

    OPEN = "OPEN"
    FIXED = "FIXED"
    INVALID_EVAL = "INVALID_EVAL"


class CaseLevelStatus(StrEnum):
    """The one status that contributes to the 80-case case-level ledger."""

    PASS = "PASS"
    OPEN_AGENT = "OPEN_AGENT"
    FIXED = "FIXED"
    INVALID_EVAL = "INVALID_EVAL"
    UNCERTAIN = "UNCERTAIN"


class CaseLedgerEntry(BaseModel):
    """One deduplicated EvalCase status and its historical root causes."""

    case_id: str
    status: CaseLevelStatus = CaseLevelStatus.UNCERTAIN
    historical_root_causes: list[str] = Field(default_factory=list)


class BadCase(BaseModel):
    """One failure from an evaluation run."""

    case_id: str = ""
    category: str = ""               # COMMAND | GOAL | REFERENCE | EXECUTION
    description: str = ""
    failure_type: FailureType = FailureType.UNKNOWN
    failure_reason: str = ""         # human-readable explanation

    input: str = ""                  # user_message
    expected: dict = Field(default_factory=dict)
    actual: dict = Field(default_factory=dict)

    trace_checks: list[dict] = Field(default_factory=list)
    # [{check: "command.type", expected: "CREATE", actual: "QUERY"}]

    # Phase 6.11 runtime regression snapshot fields.
    user_input: str = ""
    understanding_snapshot: dict | None = None
    task_plan: dict | None = None
    execution_trace: object | None = None
    expected_behavior: dict = Field(default_factory=dict)
    # Stable identity of one failed assertion.  Several assertions may share
    # one case_id; they must never become several case-level cases.
    assertion_id: str = ""
    failure_stage: str = ""
    root_cause_category: str = ""
    status: BadCaseStatus = BadCaseStatus.OPEN
    root_cause_id: str = ""


class BadCaseStore:
    """Small replaceable store for failed cases and regression snapshots."""

    def __init__(self) -> None:
        self._cases: list[BadCase] = []
        self._case_ledger: dict[str, CaseLedgerEntry] = {}

    def save(self, case: BadCase) -> BadCase:
        self._cases.append(case.model_copy(deep=True))
        self.register_case(case.case_id)
        return case

    def list_cases(self) -> list[BadCase]:
        return [case.model_copy(deep=True) for case in self._cases]

    def update_status(
        self,
        case_id: str,
        status: BadCaseStatus,
        *,
        root_cause_id: str | None = None,
        assertion_id: str | None = None,
    ) -> BadCase | None:
        """Update one assertion without creating a second store.

        A case with multiple assertions must be addressed by its stable
        ``assertion_id``.  The old single-record behavior remains valid for a
        case that has exactly one assertion.
        """

        matches = [
            index
            for index, case in enumerate(self._cases)
            if case.case_id == case_id
            and (assertion_id is None or self._assertion_key(case) == assertion_id)
        ]
        if not matches or (assertion_id is None and len(matches) != 1):
            return None
        # If the same stable assertion was observed in several runs, update
        # the latest historical observation while retaining earlier records.
        index = matches[-1]
        case = self._cases[index]
        update: dict[str, object] = {"status": status}
        if root_cause_id is not None:
            update["root_cause_id"] = root_cause_id
        updated = case.model_copy(update=update, deep=True)
        self._cases[index] = updated
        return updated.model_copy(deep=True)

    def register_case(
        self,
        case_id: str,
        status: CaseLevelStatus = CaseLevelStatus.UNCERTAIN,
        *,
        historical_root_causes: Iterable[str] = (),
    ) -> CaseLedgerEntry:
        """Register one case without overwriting an existing triage decision."""

        if not case_id:
            raise ValueError("case_id is required for case-level bookkeeping")
        existing = self._case_ledger.get(case_id)
        roots = list(dict.fromkeys(str(value) for value in historical_root_causes if value))
        if existing is None:
            entry = CaseLedgerEntry(
                case_id=case_id,
                status=status,
                historical_root_causes=roots,
            )
            self._case_ledger[case_id] = entry
            return entry.model_copy(deep=True)
        if roots:
            existing.historical_root_causes = list(
                dict.fromkeys(existing.historical_root_causes + roots)
            )
        return existing.model_copy(deep=True)

    def set_case_status(
        self,
        case_id: str,
        status: CaseLevelStatus,
        *,
        historical_root_causes: Iterable[str] = (),
    ) -> CaseLedgerEntry:
        """Set the single case-level status while retaining assertion history."""

        self.register_case(case_id, historical_root_causes=historical_root_causes)
        entry = self._case_ledger[case_id]
        entry.status = status
        if historical_root_causes:
            entry.historical_root_causes = list(
                dict.fromkeys(
                    entry.historical_root_causes
                    + [str(value) for value in historical_root_causes if value]
                )
            )
        return entry.model_copy(deep=True)

    def reconcile_cases(
        self,
        case_ids: Iterable[str],
        statuses: Mapping[str, CaseLevelStatus],
        *,
        historical_root_causes: Mapping[str, Iterable[str]] | None = None,
    ) -> list[CaseLedgerEntry]:
        """Close a case-level ledger over an explicit EvalCase universe.

        Missing status entries deliberately become ``UNCERTAIN`` rather than
        silently becoming Agent failures.  This prevents an evaluation run
        from inflating the real Agent BadCase count.
        """

        roots_by_case = historical_root_causes or {}
        entries: list[CaseLedgerEntry] = []
        for case_id in dict.fromkeys(str(value) for value in case_ids):
            status = statuses.get(case_id, CaseLevelStatus.UNCERTAIN)
            entries.append(
                self.set_case_status(
                    case_id,
                    status,
                    historical_root_causes=roots_by_case.get(case_id, ()),
                )
            )
        return entries

    def list_case_ledger(self) -> list[CaseLedgerEntry]:
        return [
            entry.model_copy(deep=True)
            for entry in sorted(self._case_ledger.values(), key=lambda item: item.case_id)
        ]

    def case_level_counts(
        self,
        case_ids: Iterable[str] | None = None,
    ) -> dict[str, int]:
        selected = (
            set(str(value) for value in case_ids)
            if case_ids is not None
            else set(self._case_ledger)
        )
        counts = {status.value: 0 for status in CaseLevelStatus}
        for case_id in selected:
            entry = self._case_ledger.get(case_id)
            if entry is not None:
                counts[entry.status.value] += 1
        return counts

    def list_current_assertions(
        self,
        *,
        case_statuses: Iterable[CaseLevelStatus] | None = None,
    ) -> list[BadCase]:
        """Return one latest record per assertion identity.

        The raw ``list_cases`` result remains historical and append-only;
        this projection is the current failure-level view used for counts.
        """

        latest: dict[str, BadCase] = {}
        for case in self._cases:
            latest[self._assertion_key(case)] = case
        allowed = set(case_statuses) if case_statuses is not None else None
        result: list[BadCase] = []
        for case in latest.values():
            entry = self._case_ledger.get(case.case_id)
            if allowed is not None and (entry is None or entry.status not in allowed):
                continue
            result.append(case.model_copy(deep=True))
        return sorted(result, key=lambda item: self._assertion_key(item))

    def open_agent_assertions(self) -> list[BadCase]:
        return [
            case
            for case in self.list_current_assertions(
                case_statuses={CaseLevelStatus.OPEN_AGENT}
            )
            if case.status == BadCaseStatus.OPEN
        ]

    @staticmethod
    def _assertion_key(case: BadCase) -> str:
        if case.assertion_id:
            return case.assertion_id
        return "{}:{}:{}".format(
            case.case_id,
            case.failure_type.value,
            case.failure_reason,
        )

    def clear(self) -> None:
        self._cases.clear()
        self._case_ledger.clear()


__all__ = [
    "FailureType",
    "BadCaseStatus",
    "CaseLevelStatus",
    "CaseLedgerEntry",
    "BadCase",
    "BadCaseStore",
]
