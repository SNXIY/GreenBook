class ModerationRepositoryError(Exception):
    """Base repository exception."""


class TaskNotFoundError(ModerationRepositoryError):
    pass


class TaskStateConflictError(ModerationRepositoryError):
    pass


class PolicyConflictError(ModerationRepositoryError):
    pass
