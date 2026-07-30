from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class IntentCatalog:
    def __init__(self, path: str | Path) -> None:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(payload, list):
            raise ValueError("Intent catalog must be a JSON array")
        self._items = {
            str(item["name"]): str(item["description"])
            for item in payload
            if isinstance(item, dict) and item.get("name") and item.get("description")
        }
        if not self._items:
            raise ValueError("Intent catalog cannot be empty")

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._items))

    def catalog_prompt(self) -> str:
        return "\n".join(
            f"- {name}: {description}"
            for name, description in sorted(self._items.items())
        )

    def as_dict(self) -> dict[str, Any]:
        return dict(self._items)


intent_catalog = IntentCatalog(Path(__file__).with_name("intent_catalog.json"))
