from agents.moderation import policy_observability


class FakeRunTree:
    def __init__(self) -> None:
        self.metadata = {}

    def add_metadata(self, values) -> None:
        self.metadata.update(values)


def test_policy_rag_metadata_is_allowlisted_redacted_and_bounded() -> None:
    metadata = policy_observability.safe_policy_rag_metadata(
        {
            "trace_name": "policy_query_planner",
            "moderation_task_id": "task-1",
            "query_history": ["Contact alice@example.com or 13812345678 about policy " + "x" * 600],
            "risk_hypotheses": ["PRIVACY"],
            "raw_content": "alice@example.com 13812345678",
        }
    )

    serialized = str(metadata)
    assert "raw_content" not in metadata
    assert "alice@example.com" not in serialized
    assert "13812345678" not in serialized
    assert "a***@example.com" in serialized
    assert "138****5678" in serialized
    assert len(metadata["query_history"][0]) <= 512


def test_policy_rag_metadata_is_attached_to_current_langsmith_run(monkeypatch) -> None:
    run_tree = FakeRunTree()
    monkeypatch.setattr(
        policy_observability,
        "get_current_run_tree",
        lambda: run_tree,
    )

    result = policy_observability.record_policy_rag_trace_metadata(
        trace_name="policy_grader",
        moderation_task_id="task-2",
        applicable_policy_count=2,
        sufficient=True,
    )

    assert run_tree.metadata == result
    assert run_tree.metadata["trace_name"] == "policy_grader"
    assert run_tree.metadata["applicable_policy_count"] == 2
