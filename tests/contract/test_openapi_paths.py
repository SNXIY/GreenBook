"""Contract tests: Verify JavaClient paths match java-openapi.yaml."""

from __future__ import annotations

import re
import yaml
import pytest


def _normalize_path(path: str) -> str:
    subs = {
        "{post_id}": "{postId}",
        "{draft_id}": "{draftId}",
        "{schedule_id}": "{scheduleId}",
        "{comment_id}": "{commentId}",
    }
    result = path.rstrip("/")
    result = re.sub(r"\{[a-z_]+\.[a-z_]+\}", "{commentId}", result)
    for py_var, java_var in subs.items():
        result = result.replace(py_var, java_var)
    return result


def _load_openapi_paths() -> dict[str, set[str]]:
    with open("contracts/java-openapi.yaml", encoding="utf-8") as f:
        spec = yaml.safe_load(f)
    ops: dict[str, set[str]] = {}
    for path, methods in spec.get("paths", {}).items():
        p = path.rstrip("/")
        ops[p] = {m.upper() for m in methods if m in ("get", "post", "put", "delete")}
    return ops


def _extract_client_paths() -> dict[str, tuple[str, str]]:
    with open("packages/java_client/greenbook_java_client/client.py", encoding="utf-8") as f:
        text = f.read()

    # Build function name → byte offset mapping for forward lookup
    func_starts: list[tuple[int, str]] = [
        (m.start(), m.group(1))
        for m in re.finditer(r"async def (\w+)", text)
    ]

    func_paths: dict[str, tuple[str, str]] = {}
    scan = 0

    while True:
        idx = text.find("self._request(", scan)
        if idx < 0:
            break

        segment = text[idx : idx + 500]
        m1 = re.search(r'"((?:GET|POST|PUT|DELETE))"', segment)
        if m1:
            method = m1.group(1)
            after = segment[m1.end() :]
            m2 = re.search(
                r'(?:f)?"((?:/[^"\s,{]*(?:\{[^}]*\})?[^"\s,]*)*)"', after
            )
            if m2:
                path = m2.group(1)
                # Find nearest preceding async def
                func_name = None
                for fstart, fname in reversed(func_starts):
                    if fstart < idx:
                        func_name = fname
                        break
                if func_name and not func_name.startswith("_"):
                    func_paths[func_name] = (method, path)

        scan = idx + 1

    return func_paths


OPENAPI_OPS = _load_openapi_paths()
CLIENT_PATHS = _extract_client_paths()


@pytest.mark.parametrize(
    "func_name,method,raw_path",
    [(name, m, p) for name, (m, p) in sorted(CLIENT_PATHS.items())],
)
def test_client_path_exists_in_openapi(func_name, method, raw_path):
    norm = _normalize_path(raw_path)
    assert norm in OPENAPI_OPS, (
        f"Client '{func_name}': '{raw_path}' → '{norm}' "
        f"not in OpenAPI: {sorted(OPENAPI_OPS.keys())}"
    )
    assert method in OPENAPI_OPS[norm], (
        f"Client '{func_name}': {method} {norm} — OpenAPI has {OPENAPI_OPS[norm]}"
    )


def test_all_openapi_paths_covered():
    covered = {_normalize_path(p) for _, (_, p) in CLIENT_PATHS.items()}
    for path, methods in sorted(OPENAPI_OPS.items()):
        for method in methods:
            found = any(
                _normalize_path(cp) == path and cm == method
                for _, (cm, cp) in CLIENT_PATHS.items()
            )
            assert found, f"OpenAPI {method} {path} has no client method"


def test_update_draft_path():
    _, raw = CLIENT_PATHS["update_draft"]
    assert "/update" in raw, f"update_draft missing /update: {raw}"


def test_schedule_status_enum():
    from greenbook_java_client.models import ScheduleStatus
    assert {s.value for s in ScheduleStatus} == {
        "SCHEDULED", "PROCESSING", "PUBLISHED", "CANCELLED", "FAILED"
    }


def test_expected_version_field():
    from greenbook_java_client.models import AgentDraftUpdateRequest
    assert "expected_version" in AgentDraftUpdateRequest.model_fields


def test_all_client_methods_valid_http():
    for func, (method, _) in CLIENT_PATHS.items():
        assert method in ("GET", "POST", "PUT", "DELETE"), f"{func}: bad {method}"
