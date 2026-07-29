"""Ensure apps/scbio is importable under the hermetic test runner."""

from __future__ import annotations

import sys
from pathlib import Path

_SCBIO = Path(__file__).resolve().parents[2] / "apps" / "scbio"
if _SCBIO.is_dir() and str(_SCBIO) not in sys.path:
    sys.path.insert(0, str(_SCBIO))
