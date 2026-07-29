"""Stage gates: verify declared outputs exist and checks pass before advancing."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Tuple

from scbio_provenance.client import load_manifest
from scbio_provenance.model import StepManifest


class GateError(Exception):
    def __init__(self, message: str, details: Dict[str, Any] | None = None):
        super().__init__(message)
        self.details = details or {}


def verify_stage(
    stage: Dict[str, Any],
    workspace: Path,
    manifest_path: Path | None = None,
) -> Tuple[bool, Dict[str, Any]]:
    """Return (ok, report). Raises GateError only when require_* and hard fail."""
    report: Dict[str, Any] = {
        "stage": stage.get("id"),
        "missing_outputs": [],
        "failed_checks": [],
        "manifest": None,
    }
    declared = stage.get("outputs") or []
    for out in declared:
        path = workspace / out["path"]
        if not path.exists():
            report["missing_outputs"].append(out["path"])

    mpath = manifest_path or (workspace / "step.manifest.json")
    # prefer stage-specific manifest if present
    stage_manifest = workspace / f"{stage.get('id')}.manifest.json"
    if stage_manifest.exists():
        mpath = stage_manifest
    if mpath.exists():
        manifest = load_manifest(mpath)
        report["manifest"] = manifest.to_dict()
        for k, v in (manifest.checks or {}).items():
            if v is False or v == "fail" or v == "failed":
                report["failed_checks"].append({k: v})
    else:
        report["manifest"] = None

    ok = not report["missing_outputs"] and not report["failed_checks"]
    return ok, report


def assert_gate(stage: Dict[str, Any], workspace: Path, gates: Dict[str, Any] | None = None) -> Dict[str, Any]:
    gates = gates or {"require_outputs": True, "require_checks": True}
    ok, report = verify_stage(stage, workspace)
    if gates.get("require_outputs") and report["missing_outputs"]:
        raise GateError(
            f"Stage {stage.get('id')} missing outputs: {report['missing_outputs']}",
            report,
        )
    if gates.get("require_checks") and report["failed_checks"]:
        raise GateError(
            f"Stage {stage.get('id')} failed checks: {report['failed_checks']}",
            report,
        )
    return report
