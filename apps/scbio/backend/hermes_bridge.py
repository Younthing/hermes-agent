"""Hermes bridge: minimal planner ping for /health demos."""

from __future__ import annotations

from typing import Any, Dict, Optional


def ping_planner(intent: str = "qc → normalize → cluster") -> Dict[str, Any]:
    """Try a one-shot Hermes chat; fall back to deterministic note."""
    try:
        from orchestrator.planner import try_hermes_plan

        text = try_hermes_plan(intent)
        if text:
            return {"source": "hermes", "text": text}
    except Exception as e:  # noqa: BLE001
        return {"source": "error", "text": str(e)}
    return {
        "source": "deterministic",
        "text": f"Plan confirmed for intent={intent!r}: qc → normalize → cluster.",
    }
