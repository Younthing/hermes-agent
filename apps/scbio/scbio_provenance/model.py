"""Event-sourced provenance model for scbio thin-slice."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class ArtifactKind(str, Enum):
    DATA = "data"
    FIGURE = "figure"
    DOC = "doc"
    METRICS = "metrics"
    PIPELINE = "pipeline"
    OTHER = "other"


class NodeKind(str, Enum):
    STEP = "step"
    ARTIFACT = "artifact"


class EdgeKind(str, Enum):
    CONSUMES = "consumes"
    PRODUCES = "produces"


class EventType(str, Enum):
    STEP_STARTED = "step_started"
    STEP_FINISHED = "step_finished"
    STEP_RETRIED = "step_retried"
    STEP_FAILED = "step_failed"
    ARTIFACT_VERSION_CREATED = "artifact_version_created"
    MANUAL_EDIT = "manual_edit"
    AI_EDIT = "ai_edit"
    INVALIDATED = "invalidated"
    PIPELINE_PLANNED = "pipeline_planned"
    STATUS_SET = "status_set"


class ImpactTier(str, Enum):
    """Impact tiers for surgical invalidation.

    T0 prose/doc — only writing/doc nodes dirty
    T1 presentation — re-render figures; data outputs unchanged
    T2 compute params — rerun stage; cascade only if data hash changes
    T3 input/upstream data — rerun affected subgraph with hash short-circuit
    """

    T0 = "T0"
    T1 = "T1"
    T2 = "T2"
    T3 = "T3"


class NodeStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    CLEAN = "clean"
    DIRTY = "dirty"
    STALE = "stale"
    FAILED = "failed"


@dataclass
class ArtifactRef:
    path: str
    hash: str
    kind: str = ArtifactKind.OTHER.value
    logical_id: Optional[str] = None  # stable artifact identity across versions

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "ArtifactRef":
        return cls(
            path=d["path"],
            hash=d["hash"],
            kind=d.get("kind", ArtifactKind.OTHER.value),
            logical_id=d.get("logical_id"),
        )


@dataclass
class StepManifest:
    """Contract emitted by stage scripts (step.manifest.json)."""

    step_id: str
    skill: str
    status: str
    started_at: str
    ended_at: str
    inputs: List[ArtifactRef] = field(default_factory=list)
    outputs: List[ArtifactRef] = field(default_factory=list)
    compute_params: Dict[str, Any] = field(default_factory=dict)
    presentation_params: Dict[str, Any] = field(default_factory=dict)
    code_hash: str = ""
    env: Dict[str, Any] = field(default_factory=dict)
    checks: Dict[str, Any] = field(default_factory=dict)
    notes: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "step_id": self.step_id,
            "skill": self.skill,
            "status": self.status,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "inputs": [i.to_dict() for i in self.inputs],
            "outputs": [o.to_dict() for o in self.outputs],
            "compute_params": self.compute_params,
            "presentation_params": self.presentation_params,
            "params": {
                "compute": self.compute_params,
                "presentation": self.presentation_params,
            },
            "code_hash": self.code_hash,
            "env": self.env,
            "checks": self.checks,
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "StepManifest":
        params = d.get("params") or {}
        compute = d.get("compute_params") or params.get("compute") or {}
        presentation = d.get("presentation_params") or params.get("presentation") or {}
        return cls(
            step_id=d["step_id"],
            skill=d["skill"],
            status=d.get("status", "ok"),
            started_at=d.get("started_at", ""),
            ended_at=d.get("ended_at", ""),
            inputs=[ArtifactRef.from_dict(x) for x in d.get("inputs", [])],
            outputs=[ArtifactRef.from_dict(x) for x in d.get("outputs", [])],
            compute_params=dict(compute),
            presentation_params=dict(presentation),
            code_hash=d.get("code_hash", ""),
            env=dict(d.get("env") or {}),
            checks=dict(d.get("checks") or {}),
            notes=d.get("notes", ""),
        )


@dataclass
class GraphSnapshot:
    """Folded graph state at a point in time."""

    nodes: List[Dict[str, Any]]
    edges: List[Dict[str, Any]]
    artifact_versions: List[Dict[str, Any]]
    as_of_event_id: Optional[int] = None
    as_of_ts: Optional[str] = None
