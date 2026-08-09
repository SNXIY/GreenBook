#!/usr/bin/env bash
# Canonical regression suite — do not compare counts across ad-hoc filters.
set -euo pipefail
cd "$(dirname "$0")/.."
PYTHON="${PYTHON:-.venv/bin/python}"
if [[ ! -x "$PYTHON" ]]; then
  PYTHON=python
fi
exec "$PYTHON" -m pytest -m "regression and not external" -q "$@"
