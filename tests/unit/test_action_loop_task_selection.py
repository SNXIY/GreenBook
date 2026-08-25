from types import SimpleNamespace

from apps.agent_api.greenbook_agent_api.services.action_loop_executor import _command_task_id


def test_create_command_does_not_reuse_active_task() -> None:
    command = SimpleNamespace(type="CREATE", resolved_target=None)
    session = SimpleNamespace(active_task_id="completed-task")
    assert _command_task_id(command, session) == ""


def test_modify_command_reuses_active_task() -> None:
    command = SimpleNamespace(type="MODIFY", resolved_target=None)
    session = SimpleNamespace(active_task_id="existing-task")
    assert _command_task_id(command, session) == "existing-task"
