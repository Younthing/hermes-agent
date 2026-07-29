"""Client helpers for stage scripts: hash files + emit step.manifest.json."""

from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from .model import ArtifactKind, ArtifactRef, StepManifest
from .store import ProvenanceStore, sha256_file, utcnow


def hash_path(path: Union[str, Path]) -> str:
    return sha256_file(Path(path))


def code_hash_of(paths: List[Union[str, Path]]) -> str:
    h = hashlib.sha256()
    for p in sorted(str(x) for x in paths):
        path = Path(p)
        if path.exists() and path.is_file():
            h.update(path.read_bytes())
        else:
            h.update(p.encode())
    return h.hexdigest()


def env_snapshot() -> Dict[str, Any]:
    return {
        "python": sys.version.split()[0],
        "platform": sys.platform,
        "SCBIO_PROJECT": os.environ.get("SCBIO_PROJECT", ""),
        "cwd": os.getcwd(),
        "has_scanpy": _has_mod("scanpy"),
        "has_anndata": _has_mod("anndata"),
        "has_numpy": _has_mod("numpy"),
    }


def _has_mod(name: str) -> bool:
    try:
        __import__(name)
        return True
    except Exception:
        return False


class StepRecorder:
    """Context manager used by stage scripts."""

    def __init__(
        self,
        step_id: str,
        skill: str,
        out_dir: Union[str, Path],
        compute_params: Optional[Dict[str, Any]] = None,
        presentation_params: Optional[Dict[str, Any]] = None,
        code_paths: Optional[List[Union[str, Path]]] = None,
    ):
        self.step_id = step_id
        self.skill = skill
        self.out_dir = Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.compute_params = dict(compute_params or {})
        self.presentation_params = dict(presentation_params or {})
        self.code_paths = list(code_paths or [])
        self.inputs: List[ArtifactRef] = []
        self.outputs: List[ArtifactRef] = []
        self.checks: Dict[str, Any] = {}
        self.notes = ""
        self.started_at = ""
        self.ended_at = ""
        self.status = "ok"

    def add_input(
        self,
        path: Union[str, Path],
        kind: str = ArtifactKind.DATA.value,
        logical_id: Optional[str] = None,
    ) -> ArtifactRef:
        p = Path(path)
        ref = ArtifactRef(
            path=str(p),
            hash=hash_path(p) if p.exists() else "",
            kind=kind,
            logical_id=logical_id or f"art:{p.stem}",
        )
        self.inputs.append(ref)
        return ref

    def add_output(
        self,
        path: Union[str, Path],
        kind: str = ArtifactKind.DATA.value,
        logical_id: Optional[str] = None,
    ) -> ArtifactRef:
        p = Path(path)
        ref = ArtifactRef(
            path=str(p),
            hash=hash_path(p) if p.exists() else "",
            kind=kind,
            logical_id=logical_id or f"art:{p.stem}",
        )
        self.outputs.append(ref)
        return ref

    def __enter__(self) -> "StepRecorder":
        self.started_at = utcnow()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.ended_at = utcnow()
        if exc_type is not None:
            self.status = "failed"
            self.notes = f"{exc_type.__name__}: {exc}"
        manifest = self.write_manifest()
        # Auto-record only when explicitly requested. The orchestrator owns
        # recording by default (avoids duplicate versions).
        project = os.environ.get("SCBIO_PROJECT")
        if project and os.environ.get("SCBIO_AUTO_RECORD", "").lower() in (
            "1",
            "true",
            "yes",
        ):
            try:
                store = ProvenanceStore(Path(project))
                store.record_step_manifest(manifest)
                store.close()
            except Exception as e:  # noqa: BLE001 — scripts must still finish
                print(f"[scbio] warning: failed to record provenance: {e}", file=sys.stderr)
        return False

    def write_manifest(self, path: Optional[Path] = None) -> StepManifest:
        manifest = StepManifest(
            step_id=self.step_id,
            skill=self.skill,
            status=self.status,
            started_at=self.started_at or utcnow(),
            ended_at=self.ended_at or utcnow(),
            inputs=self.inputs,
            outputs=self.outputs,
            compute_params=self.compute_params,
            presentation_params=self.presentation_params,
            code_hash=code_hash_of(self.code_paths) if self.code_paths else "",
            env=env_snapshot(),
            checks=self.checks,
            notes=self.notes,
        )
        dest = path or (self.out_dir / "step.manifest.json")
        dest.write_text(json.dumps(manifest.to_dict(), indent=2), encoding="utf-8")
        return manifest


def load_manifest(path: Union[str, Path]) -> StepManifest:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return StepManifest.from_dict(data)
