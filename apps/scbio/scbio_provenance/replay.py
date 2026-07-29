"""Fold event log into a graph snapshot at time T (timeline scrubbing)."""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from .model import EventType, GraphSnapshot, NodeKind, NodeStatus
from .store import ProvenanceStore


def replay_to(
    store: ProvenanceStore,
    until_event_id: Optional[int] = None,
    until_ts: Optional[str] = None,
) -> GraphSnapshot:
    """Rebuild graph state by folding events up to T.

    For the thin slice we keep live tables as the source of truth for the
    *latest* state, and for historical scrubbing we reconstruct a projected
    view from the event log (nodes/edges/versions mentioned in events).
    """
    events = store.list_events(until_event_id=until_event_id, until_ts=until_ts)
    nodes: Dict[str, Dict[str, Any]] = {}
    edges: Dict[str, Dict[str, Any]] = {}
    versions: Dict[str, Dict[str, Any]] = {}
    edge_seq = 0

    def ensure_node(nid: str, kind: str, label: str, **extra: Any) -> None:
        if nid not in nodes:
            nodes[nid] = {
                "id": nid,
                "kind": kind,
                "label": label,
                "status": NodeStatus.PENDING.value,
                "meta": {},
            }
        nodes[nid].update({k: v for k, v in extra.items() if k != "meta"})
        if "meta" in extra:
            nodes[nid]["meta"] = {**nodes[nid].get("meta", {}), **extra["meta"]}

    for ev in events:
        t = ev["type"]
        p = ev["payload"] or {}

        if t == EventType.PIPELINE_PLANNED.value:
            ensure_node(
                "pipeline:main",
                NodeKind.ARTIFACT.value,
                "pipeline.yaml",
                status=NodeStatus.CLEAN.value,
                meta={"kind": "pipeline"},
            )
            if p.get("version_id"):
                versions[p["version_id"]] = {
                    "id": p["version_id"],
                    "logical_id": "pipeline:main",
                    "path": p.get("path", "pipeline.yaml"),
                    "content_hash": p.get("content_hash", ""),
                    "kind": "pipeline",
                    "parent_version_id": p.get("parent_version_id"),
                }

        elif t == EventType.STEP_STARTED.value:
            sid = f"step:{p['step_id']}"
            ensure_node(
                sid,
                NodeKind.STEP.value,
                p.get("skill", p["step_id"]),
                skill=p.get("skill"),
                status=NodeStatus.RUNNING.value,
            )

        elif t in (EventType.STEP_FINISHED.value, EventType.STEP_FAILED.value):
            sid = f"step:{p['step_id']}"
            st = (
                NodeStatus.CLEAN.value
                if t == EventType.STEP_FINISHED.value
                else NodeStatus.FAILED.value
            )
            ensure_node(
                sid,
                NodeKind.STEP.value,
                p.get("skill", p["step_id"]),
                skill=p.get("skill"),
                status=st,
                meta={
                    "compute_params": p.get("compute_params", {}),
                    "presentation_params": p.get("presentation_params", {}),
                },
            )
            for out in p.get("outputs") or []:
                logical = out.get("logical_id")
                if not logical:
                    continue
                ensure_node(
                    logical,
                    NodeKind.ARTIFACT.value,
                    logical.split(":")[-1],
                    status=NodeStatus.CLEAN.value,
                    meta={"kind": out.get("kind"), "path": out.get("path")},
                )
                edge_seq += 1
                eid = f"replay_e_{edge_seq}"
                edges[eid] = {
                    "id": eid,
                    "kind": "produces",
                    "src_id": sid,
                    "dst_id": logical,
                }
                if out.get("version_id"):
                    versions[out["version_id"]] = {
                        "id": out["version_id"],
                        "logical_id": logical,
                        "path": out.get("path", ""),
                        "content_hash": out.get("content_hash", ""),
                        "kind": out.get("kind", "other"),
                        "parent_version_id": out.get("parent_version_id"),
                    }

        elif t == EventType.ARTIFACT_VERSION_CREATED.value:
            logical = p["logical_id"]
            ensure_node(
                logical,
                NodeKind.ARTIFACT.value,
                logical.split(":")[-1],
                status=NodeStatus.CLEAN.value,
                meta={"kind": p.get("kind"), "path": p.get("path")},
            )
            versions[p["version_id"]] = {
                "id": p["version_id"],
                "logical_id": logical,
                "path": p.get("path", ""),
                "content_hash": p.get("content_hash", ""),
                "kind": p.get("kind", "other"),
                "parent_version_id": p.get("parent_version_id"),
            }

        elif t == EventType.STATUS_SET.value:
            nid = p.get("node_id")
            if nid and nid in nodes:
                nodes[nid]["status"] = p.get("status", nodes[nid]["status"])

        elif t == EventType.INVALIDATED.value:
            for nid in p.get("dirty_nodes") or []:
                if nid in nodes:
                    nodes[nid]["status"] = NodeStatus.DIRTY.value
                else:
                    ensure_node(
                        nid,
                        NodeKind.STEP.value if nid.startswith("step:") else NodeKind.ARTIFACT.value,
                        nid.split(":")[-1],
                        status=NodeStatus.DIRTY.value,
                    )

        elif t in (EventType.MANUAL_EDIT.value, EventType.AI_EDIT.value):
            logical = p.get("logical_id")
            if logical:
                ensure_node(
                    logical,
                    NodeKind.ARTIFACT.value,
                    logical.split(":")[-1],
                    status=NodeStatus.CLEAN.value,
                    meta={"edit": t, "note": p.get("note", "")},
                )
                if p.get("version_id"):
                    versions[p["version_id"]] = {
                        "id": p["version_id"],
                        "logical_id": logical,
                        "path": p.get("path", ""),
                        "content_hash": p.get("content_hash", ""),
                        "kind": p.get("kind", "doc"),
                        "parent_version_id": p.get("parent_version_id"),
                    }

    last = events[-1] if events else None
    return GraphSnapshot(
        nodes=list(nodes.values()),
        edges=list(edges.values()),
        artifact_versions=list(versions.values()),
        as_of_event_id=last["id"] if last else None,
        as_of_ts=last["ts"] if last else None,
    )


def events_for_timeline(store: ProvenanceStore) -> List[Dict[str, Any]]:
    """Compact event list for the UI timeline scrubber."""
    events = store.list_events()
    out = []
    for ev in events:
        out.append(
            {
                "id": ev["id"],
                "ts": ev["ts"],
                "type": ev["type"],
                "summary": _summarize(ev),
                "payload": ev["payload"],
            }
        )
    return out


def _summarize(ev: Dict[str, Any]) -> str:
    t = ev["type"]
    p = ev.get("payload") or {}
    if t == EventType.STEP_FINISHED.value:
        return f"Finished {p.get('skill', p.get('step_id'))}"
    if t == EventType.STEP_STARTED.value:
        return f"Started {p.get('skill', p.get('step_id'))}"
    if t == EventType.STEP_FAILED.value:
        return f"Failed {p.get('skill', p.get('step_id'))}"
    if t == EventType.ARTIFACT_VERSION_CREATED.value:
        return f"Artifact {p.get('logical_id')} @ {str(p.get('content_hash', ''))[:8]}"
    if t == EventType.INVALIDATED.value:
        return f"Invalidated {len(p.get('dirty_nodes') or [])} nodes ({p.get('tier')})"
    if t == EventType.MANUAL_EDIT.value:
        return f"Manual edit {p.get('logical_id')}"
    if t == EventType.AI_EDIT.value:
        return f"AI edit {p.get('logical_id')}"
    if t == EventType.PIPELINE_PLANNED.value:
        return "Pipeline planned"
    return t
