from types import SimpleNamespace

from greenbook_agent_core.execution.execution_queue import ExecutionQueueMessage
from greenbook_agent_core.execution.queue_execution_handler import _inject_test_result_unknown
from greenbook_agent_core.execution.runtime_result import RuntimeResult


def _message(execution_id: str = "exec-1") -> ExecutionQueueMessage:
    return ExecutionQueueMessage(
        execution_id=execution_id,
        payload={},
        trace_id="trace-1",
    )


def test_result_unknown_fault_is_disabled_by_default(monkeypatch):
    monkeypatch.delenv("GREENBOOK_ENV", raising=False)
    monkeypatch.setenv("GREENBOOK_ALLOW_TEST_FAULTS", "true")
    monkeypatch.setenv("GREENBOOK_TEST_RESULT_UNKNOWN_ONCE", "*")
    result = RuntimeResult(status="COMPLETED", success=True)
    assert _inject_test_result_unknown(_message(), result) is result


def test_result_unknown_fault_is_one_shot_and_test_only(monkeypatch):
    monkeypatch.setenv("GREENBOOK_ENV", "test")
    monkeypatch.setenv("GREENBOOK_ALLOW_TEST_FAULTS", "true")
    monkeypatch.setenv("GREENBOOK_TEST_RESULT_UNKNOWN_ONCE", "exec-fault")
    first = _inject_test_result_unknown(_message("exec-fault"), RuntimeResult(status="COMPLETED", success=True))
    second = _inject_test_result_unknown(_message("exec-fault"), RuntimeResult(status="COMPLETED", success=True))
    assert first.status == "RESULT_UNKNOWN"
    assert first.success is False
    assert second.status == "COMPLETED"
