from __future__ import annotations


class CreatorHarnessError(RuntimeError):
    code = "CREATOR_HARNESS_ERROR"
    retryable = False

    def __init__(self, message: str = "", *, details: dict | None = None):
        super().__init__(message or self.code)
        self.details = details or {}


class CreatorTaskNotFoundError(CreatorHarnessError):
    code = "TASK_NOT_FOUND"


class CreatorRunNotFoundError(CreatorHarnessError):
    code = "RUN_NOT_FOUND"


class CreatorScopeViolationError(CreatorHarnessError):
    code = "TASK_SCOPE_VIOLATION"


class CreatorTaskVersionConflictError(CreatorHarnessError):
    code = "TASK_VERSION_CONFLICT"


class CreatorInvalidTransitionError(CreatorHarnessError):
    code = "TASK_INVALID_TRANSITION"


class CreatorIdempotencyConflictError(CreatorHarnessError):
    code = "IDEMPOTENCY_KEY_REUSED"


class CreatorPersistenceConflictError(CreatorHarnessError):
    code = "PERSISTENCE_CONFLICT"
    retryable = True


class CreatorArtifactConflictError(CreatorHarnessError):
    code = "ARTIFACT_IMMUTABILITY_CONFLICT"


class CreatorDecisionNotFoundError(CreatorHarnessError):
    code = "DECISION_NOT_FOUND"


class CreatorDecisionConflictError(CreatorHarnessError):
    code = "DECISION_STATE_CONFLICT"


class CreatorCheckpointConflictError(CreatorHarnessError):
    code = "CHECKPOINT_CONFLICT"


class CreatorRunLeaseConflictError(CreatorHarnessError):
    code = "RUN_LEASE_CONFLICT"
    retryable = True


class CreatorStaleWorkerResultError(CreatorHarnessError):
    code = "STALE_WORKER_RESULT"


class CreatorRuntimeContractError(CreatorHarnessError):
    code = "RUNTIME_CONTRACT_ERROR"


class CreatorRuntimeRetryableError(CreatorHarnessError):
    code = "RUNTIME_RETRYABLE_ERROR"
    retryable = True

    def __init__(
        self,
        message: str,
        *,
        error_code: str = "RUNTIME_RETRYABLE_ERROR",
        details: dict | None = None,
    ):
        super().__init__(message, details=details)
        self.error_code = error_code


class CreatorRuntimeFatalError(CreatorHarnessError):
    code = "RUNTIME_FATAL_ERROR"

    def __init__(
        self,
        message: str,
        *,
        error_code: str = "RUNTIME_FATAL_ERROR",
        details: dict | None = None,
    ):
        super().__init__(message, details=details)
        self.error_code = error_code
