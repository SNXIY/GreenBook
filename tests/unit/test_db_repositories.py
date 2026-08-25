from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

from greenbook_agent_core.db.repositories import _row_to_dict


def test_row_to_dict_accepts_mapping_and_dict() -> None:
    approval_id = uuid4()
    assert _row_to_dict({"approval_id": approval_id})["approval_id"] == str(approval_id)
    assert _row_to_dict({"status": "PENDING"}) == {"status": "PENDING"}


def test_row_to_dict_accepts_sqlalchemy_row_mapping() -> None:
    approval_id = uuid4()
    row = SimpleNamespace(_mapping={"approval_id": approval_id})
    assert _row_to_dict(row)["approval_id"] == str(approval_id)
