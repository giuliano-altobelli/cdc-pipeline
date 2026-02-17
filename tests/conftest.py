"""
This file configures pytest.

This file is in the root since it can be used for tests in any place in this
project, including tests under resources/.

First deactivate current python environment
cd services
uv sync --project services --group dev
source .venv/bin/activate
uv run pytest -q tests
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"

for candidate in (PROJECT_ROOT, SRC_ROOT):
    candidate_str = str(candidate)
    if candidate_str not in sys.path:
        sys.path.insert(0, candidate_str)
